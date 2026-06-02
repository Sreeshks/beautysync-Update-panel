import os
import psycopg2
from psycopg2.extras import DictCursor
from datetime import datetime, timezone, timedelta

# Indian Standard Time (UTC+5:30) — admin panel dates are entered in IST
IST = timezone(timedelta(hours=5, minutes=30))
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import requests

# Load local environment variables from .env file (if present)
load_dotenv()

app = Flask(__name__, template_folder='templates', static_folder='static')
CORS(app)  # Enable Cross-Origin Resource Sharing for mobile integration

# Configurations
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
ALLOWED_VIDEO_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv', 'webm'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB max for multiple files

# Ensure upload directory exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Supabase Storage Configuration
SUPABASE_URL = "https://ndxxzpfmnrhlbirhgbsw.supabase.co"
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
BUCKET_NAME = "campaign-media"

def upload_to_supabase(file_data, filename, content_type):
    if not SUPABASE_KEY:
        raise ValueError("SUPABASE_KEY environment variable is not set!")
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET_NAME}/{filename}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": content_type
    }
    response = requests.post(url, headers=headers, data=file_data)
    if response.status_code == 200:
        return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/{filename}"
    else:
        raise Exception(f"Failed to upload to Supabase Storage: {response.text}")

def delete_from_supabase(file_urls):
    if not SUPABASE_KEY or not file_urls:
        return
    filenames = []
    prefix = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/"
    for url in file_urls:
        if url and url.startswith(prefix):
            fn = url.replace(prefix, "")
            filenames.append(fn)
    if not filenames:
        return
    delete_url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET_NAME}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }
    body = {
        "prefixes": filenames
    }
    try:
        requests.delete(delete_url, headers=headers, json=body)
    except Exception as e:
        print("Failed to delete files from Supabase Storage:", e)

def init_supabase_storage():
    if not SUPABASE_KEY:
        return
    bucket_url = f"{SUPABASE_URL}/storage/v1/bucket/{BUCKET_NAME}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }
    try:
        response = requests.get(bucket_url, headers=headers)
        if response.status_code != 200:
            create_url = f"{SUPABASE_URL}/storage/v1/bucket"
            body = {
                "id": BUCKET_NAME,
                "name": BUCKET_NAME,
                "public": True
            }
            requests.post(create_url, headers=headers, json=body)
    except Exception as e:
        print("Failed to initialize Supabase Storage bucket:", e)

import urllib.parse as urlparse

# Supabase PostgreSQL Connection configuration
DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set!")

# Helper function to get database connection
def get_db():
    url = urlparse.urlparse(DATABASE_URL)
    user = urlparse.unquote(url.username) if url.username else None
    password = urlparse.unquote(url.password) if url.password else None
    
    conn = psycopg2.connect(
        dbname=url.path[1:],
        user=user,
        password=password,
        host=url.hostname,
        port=url.port,
        cursor_factory=DictCursor
    )
    return conn

# Initialize Database Schema
def init_db():
    conn = get_db()
    cursor = conn.cursor()

    # Legacy campaigns table (kept for backward compatibility)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS campaigns (
            id SERIAL PRIMARY KEY,
            version_code VARCHAR(255) NOT NULL,
            image_filename VARCHAR(255),
            video_filename VARCHAR(255),
            start_date VARCHAR(255) NOT NULL,
            end_date VARCHAR(255) NOT NULL,
            created_at VARCHAR(255) NOT NULL,
            api_hits INTEGER DEFAULT 0,
            image_description TEXT,
            video_description TEXT
        )
    ''')

    # New: campaign_media table for multiple media items per campaign
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS campaign_media (
            id SERIAL PRIMARY KEY,
            campaign_id INTEGER NOT NULL,
            media_type VARCHAR(50) NOT NULL,
            filename VARCHAR(255) NOT NULL,
            description TEXT,
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
        )
    ''')

    conn.commit()
    cursor.close()
    conn.close()
    
    # Initialize Supabase Storage Bucket
    init_supabase_storage()

init_db()

# Helper validators
def allowed_file(filename, allowed_set):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_set

def get_media_for_campaign(campaign_id, host_url):
    """Retrieve all media items for a given campaign."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT * FROM campaign_media WHERE campaign_id = %s ORDER BY sort_order, id',
        (campaign_id,)
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    media = []
    for m in rows:
        filename = m['filename']
        if filename.startswith('http://') or filename.startswith('https://'):
            url = filename
        else:
            url = f"{host_url}uploads/{filename}"

        media.append({
            'id': m['id'],
            'type': m['media_type'],
            'url': url,
            'filename': filename,
            'description': m['description'] or ''
        })
    return media

# Routing Admin UI
@app.route('/')
def index():
    return render_template('index.html')

# Serving Uploaded Files
@app.route('/uploads/<filename>')
def serve_upload(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# Create Campaign Endpoint (supports multiple images + videos)
@app.route('/api/updates', methods=['POST'])
def create_update():
    try:
        version_code = request.form.get('version_code', '').strip()
        start_date = request.form.get('start_date', '').strip()
        end_date = request.form.get('end_date', '').strip()

        if not version_code or not start_date or not end_date:
            return jsonify({
                'status': 'error',
                'message': 'Missing required fields: version_code, start_date, end_date.'
            }), 400

        # Multiple descriptions from dynamic form
        image_descriptions = request.form.getlist('image_descriptions')
        video_descriptions = request.form.getlist('video_descriptions')

        # Multiple file uploads
        image_files = request.files.getlist('images')
        video_files = request.files.getlist('videos')

        # Legacy single-file fields (backward compatibility)
        legacy_image = request.files.get('image')
        legacy_video = request.files.get('video')
        legacy_image_desc = request.form.get('image_description', '').strip()
        legacy_video_desc = request.form.get('video_description', '').strip()

        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')

        # Process and collect saved files: list of (filename, description)
        saved_images = []
        saved_videos = []

        # Handle multiple images
        for i, img_file in enumerate(image_files):
            if img_file and img_file.filename:
                if not allowed_file(img_file.filename, ALLOWED_IMAGE_EXTENSIONS):
                    return jsonify({
                        'status': 'error',
                        'message': f'Invalid image format for file #{i + 1}. Allowed: PNG, JPG, JPEG, GIF, WEBP.'
                    }), 400
                safe_name = f"img_{timestamp}_{i}_{secure_filename(img_file.filename)}"
                file_data = img_file.read()
                content_type = img_file.content_type or 'image/png'
                public_url = upload_to_supabase(file_data, safe_name, content_type)
                desc = image_descriptions[i].strip() if i < len(image_descriptions) else ''
                saved_images.append((public_url, desc))

        # Handle multiple videos
        for i, vid_file in enumerate(video_files):
            if vid_file and vid_file.filename:
                if not allowed_file(vid_file.filename, ALLOWED_VIDEO_EXTENSIONS):
                    return jsonify({
                        'status': 'error',
                        'message': f'Invalid video format for file #{i + 1}. Allowed: MP4, MOV, AVI, MKV, WEBM.'
                    }), 400
                safe_name = f"vid_{timestamp}_{i}_{secure_filename(vid_file.filename)}"
                file_data = vid_file.read()
                content_type = vid_file.content_type or 'video/mp4'
                public_url = upload_to_supabase(file_data, safe_name, content_type)
                desc = video_descriptions[i].strip() if i < len(video_descriptions) else ''
                saved_videos.append((public_url, desc))

        # Legacy single-file fallback
        if not saved_images and legacy_image and legacy_image.filename:
            if allowed_file(legacy_image.filename, ALLOWED_IMAGE_EXTENSIONS):
                safe_name = f"img_{timestamp}_{secure_filename(legacy_image.filename)}"
                file_data = legacy_image.read()
                content_type = legacy_image.content_type or 'image/png'
                public_url = upload_to_supabase(file_data, safe_name, content_type)
                saved_images.append((public_url, legacy_image_desc))

        if not saved_videos and legacy_video and legacy_video.filename:
            if allowed_file(legacy_video.filename, ALLOWED_VIDEO_EXTENSIONS):
                safe_name = f"vid_{timestamp}_{secure_filename(legacy_video.filename)}"
                file_data = legacy_video.read()
                content_type = legacy_video.content_type or 'video/mp4'
                public_url = upload_to_supabase(file_data, safe_name, content_type)
                saved_videos.append((public_url, legacy_video_desc))

        # For backward compat, store first image/video in campaigns table
        first_img_fn = saved_images[0][0] if saved_images else None
        first_vid_fn = saved_videos[0][0] if saved_videos else None
        first_img_desc = saved_images[0][1] if saved_images else ''
        first_vid_desc = saved_videos[0][1] if saved_videos else ''

        # Save campaign record
        conn = get_db()
        cursor = conn.cursor()
        created_at = datetime.utcnow().isoformat()
        cursor.execute('''
            INSERT INTO campaigns (version_code, image_filename, video_filename,
                                   start_date, end_date, created_at,
                                   image_description, video_description)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        ''', (version_code, first_img_fn, first_vid_fn,
              start_date, end_date, created_at,
              first_img_desc, first_vid_desc))
        campaign_id = cursor.fetchone()[0]

        # Save all media items into campaign_media table
        sort_order = 0
        for fn, desc in saved_images:
            cursor.execute('''
                INSERT INTO campaign_media (campaign_id, media_type, filename, description, sort_order)
                VALUES (%s, 'image', %s, %s, %s)
            ''', (campaign_id, fn, desc, sort_order))
            sort_order += 1

        for fn, desc in saved_videos:
            cursor.execute('''
                INSERT INTO campaign_media (campaign_id, media_type, filename, description, sort_order)
                VALUES (%s, 'video', %s, %s, %s)
            ''', (campaign_id, fn, desc, sort_order))
            sort_order += 1

        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({'status': 'success', 'message': 'Campaign uploaded successfully.'}), 201

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# Get All Campaigns Endpoint (Admin)
@app.route('/api/updates', methods=['GET'])
def list_updates():
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM campaigns ORDER BY created_at DESC')
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        host_url = request.host_url
        campaigns = []
        for r in rows:
            # Fetch full media list for this campaign
            media = get_media_for_campaign(r['id'], host_url)

            # Backward compatible first-image/video fields
            first_image = next((m for m in media if m['type'] == 'image'), None)
            first_video = next((m for m in media if m['type'] == 'video'), None)

            # If no media rows exist, fall back to legacy campaign columns
            if not first_image and r['image_filename']:
                first_image = {
                    'type': 'image',
                    'url': f"{host_url}uploads/{r['image_filename']}",
                    'filename': r['image_filename'],
                    'description': r['image_description'] or ''
                }
                if not media:
                    media.append(first_image)

            if not first_video and r['video_filename']:
                first_video = {
                    'type': 'video',
                    'url': f"{host_url}uploads/{r['video_filename']}",
                    'filename': r['video_filename'],
                    'description': r['video_description'] or ''
                }
                if not any(m.get('type') == 'video' for m in media):
                    media.append(first_video)

            campaigns.append({
                'id': r['id'],
                'version_code': r['version_code'],
                # Backward compat single fields
                'image_url': first_image['url'] if first_image else None,
                'image_filename': first_image['filename'] if first_image else None,
                'image_description': first_image['description'] if first_image else None,
                'video_url': first_video['url'] if first_video else None,
                'video_filename': first_video['filename'] if first_video else None,
                'video_description': first_video['description'] if first_video else None,
                # New multi-media array
                'media': media,
                'start_date': r['start_date'],
                'end_date': r['end_date'],
                'created_at': r['created_at'],
                'api_hits': r['api_hits']
            })

        return jsonify({'status': 'success', 'data': campaigns})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# Delete Campaign Endpoint
@app.route('/api/updates/<int:campaign_id>', methods=['DELETE'])
def delete_update(campaign_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM campaigns WHERE id = %s', (campaign_id,))
        campaign = cursor.fetchone()

        if not campaign:
            cursor.close()
            conn.close()
            return jsonify({'status': 'error', 'message': 'Campaign not found.'}), 404

        # Delete all media files from campaign_media table (supports both local and Supabase)
        cursor.execute('SELECT filename FROM campaign_media WHERE campaign_id = %s', (campaign_id,))
        media_rows = cursor.fetchall()
        
        supabase_urls_to_delete = []
        for m in media_rows:
            filename = m['filename']
            if filename.startswith('http://') or filename.startswith('https://'):
                supabase_urls_to_delete.append(filename)
            else:
                fpath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                if os.path.exists(fpath):
                    os.remove(fpath)

        # Delete legacy files from campaigns table (if they weren't in campaign_media)
        if campaign['image_filename']:
            filename = campaign['image_filename']
            if filename.startswith('http://') or filename.startswith('https://'):
                supabase_urls_to_delete.append(filename)
            else:
                img_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                if os.path.exists(img_path):
                    os.remove(img_path)

        if campaign['video_filename']:
            filename = campaign['video_filename']
            if filename.startswith('http://') or filename.startswith('https://'):
                supabase_urls_to_delete.append(filename)
            else:
                vid_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                if os.path.exists(vid_path):
                    os.remove(vid_path)

        # Bulk delete from Supabase Storage
        if supabase_urls_to_delete:
            try:
                delete_from_supabase(supabase_urls_to_delete)
            except Exception as e:
                print("Failed to delete from Supabase Storage:", e)

        # Delete campaign_media rows (cascade should handle this but be explicit)
        cursor.execute('DELETE FROM campaign_media WHERE campaign_id = %s', (campaign_id,))
        cursor.execute('DELETE FROM campaigns WHERE id = %s', (campaign_id,))
        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({'status': 'success', 'message': 'Campaign deleted successfully.'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# Mobile Active Update GET API Endpoint
# Mobile apps will call: GET /api/mobile/active-update?version_code=104
@app.route('/api/mobile/active-update', methods=['GET'])
def mobile_active_update():
    try:
        version_code = request.args.get('version_code', '').strip()
        current_time = datetime.now(IST).strftime('%Y-%m-%dT%H:%M')

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM campaigns 
            WHERE start_date <= %s AND end_date >= %s
            ORDER BY created_at DESC
        ''', (current_time, current_time))
        rows = cursor.fetchall()

        if not rows:
            cursor.close()
            conn.close()
            return jsonify({
                'status': 'success',
                'has_update': False,
                'message': 'No active campaigns at this time.',
                'update': None
            })

        # Match campaign by version code
        selected_campaign = None
        if version_code:
            for r in rows:
                if r['version_code'] == version_code:
                    selected_campaign = r
                    break
            if not selected_campaign:
                for r in rows:
                    if r['version_code'].lower() in ('all', '*', '0', 'any'):
                        selected_campaign = r
                        break

        if not selected_campaign:
            selected_campaign = rows[0]

        # Increment API hits
        cursor.execute(
            'UPDATE campaigns SET api_hits = api_hits + 1 WHERE id = %s',
            (selected_campaign['id'],)
        )
        conn.commit()
        cursor.close()
        conn.close()

        # Get all media for this campaign
        host_url = request.host_url
        media = get_media_for_campaign(selected_campaign['id'], host_url)

        # Backward compat: first image/video from media array or legacy columns
        first_image = next((m for m in media if m['type'] == 'image'), None)
        first_video = next((m for m in media if m['type'] == 'video'), None)

        if not first_image and selected_campaign['image_filename']:
            first_image = {
                'type': 'image',
                'url': f"{host_url}uploads/{selected_campaign['image_filename']}",
                'description': selected_campaign['image_description'] or ''
            }
            if not media:
                media.append(first_image)

        if not first_video and selected_campaign['video_filename']:
            first_video = {
                'type': 'video',
                'url': f"{host_url}uploads/{selected_campaign['video_filename']}",
                'description': selected_campaign['video_description'] or ''
            }
            if not any(m.get('type') == 'video' for m in media):
                media.append(first_video)

        return jsonify({
            'status': 'success',
            'has_update': True,
            'update': {
                'id': selected_campaign['id'],
                'version_code': selected_campaign['version_code'],
                # Backward compat single fields
                'image_url': first_image['url'] if first_image else None,
                'image_description': first_image['description'] if first_image else None,
                'video_url': first_video['url'] if first_video else None,
                'video_description': first_video['description'] if first_video else None,
                # New multi-media array
                'media': media,
                'start_date': selected_campaign['start_date'],
                'end_date': selected_campaign['end_date'],
                'created_at': selected_campaign['created_at']
            }
        })

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# Administrative Dashboard Stats API
@app.route('/api/stats', methods=['GET'])
def get_stats():
    try:
        current_time = datetime.now(IST).strftime('%Y-%m-%dT%H:%M')
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute('SELECT COUNT(*) as total FROM campaigns')
        total = cursor.fetchone()['total']

        cursor.execute(
            'SELECT COUNT(*) as active FROM campaigns WHERE start_date <= %s AND end_date >= %s',
            (current_time, current_time)
        )
        active = cursor.fetchone()['active']

        cursor.execute('SELECT SUM(api_hits) as hits FROM campaigns')
        hits_row = cursor.fetchone()
        total_hits = hits_row['hits'] if hits_row['hits'] is not None else 0

        # Total media items across all campaigns
        cursor.execute('SELECT COUNT(*) as media_count FROM campaign_media')
        media_count = cursor.fetchone()['media_count']

        cursor.close()
        conn.close()

        return jsonify({
            'status': 'success',
            'stats': {
                'total_campaigns': total,
                'active_campaigns': active,
                'total_api_hits': total_hits,
                'total_media_items': media_count
            }
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
