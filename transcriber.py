import yt_dlp
import os
import shutil
import re  # For VTT parsing and filename sanitization
from flask import Flask, request, jsonify, send_from_directory, url_for
import uuid  # For unique temporary transcript file names
from datetime import datetime  # For timestamped filenames

# --- Flask App Setup ---
app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# --- Directory Configuration ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_BASE_DIR = os.path.join(BASE_DIR, 'api_downloads')  # For MP3s
TRANSCRIPTS_TEMP_DIR = os.path.join(BASE_DIR, 'api_transcripts_temp')  # For temporary VTT files
os.makedirs(DOWNLOADS_BASE_DIR, exist_ok=True)
os.makedirs(TRANSCRIPTS_TEMP_DIR, exist_ok=True)

# --- Constants ---
SOCKET_TIMEOUT_SECONDS = 180
COMMON_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
)

# --- Helpers ---

def is_ffmpeg_available():
    return shutil.which('ffmpeg') is not None


def sanitize_filename(name_str, max_length=60):
    s = (name_str or '').replace(' ', '_')
    s = ''.join(c if c.isalnum() or c in ('-', '_') else '_' for c in s)
    s = re.sub(r'_+', '_', s).strip('_')
    return s[:max_length]


def vtt_to_plaintext(vtt_content):
    lines = vtt_content.splitlines()
    segments = []
    buffer = []
    for line in lines:
        text = line.strip()
        if not text or text.startswith('WEBVTT') or '-->' in text or text.isdigit():
            if buffer:
                segments.append(' '.join(buffer))
                buffer = []
            continue
        buffer.append(re.sub(r'<[^>]+>', '', text))
    if buffer:
        segments.append(' '.join(buffer))
    # Deduplicate consecutive segments
    dedup = []
    for seg in segments:
        if not dedup or seg != dedup[-1]:
            dedup.append(seg)
    return '\n'.join(dedup)


def fetch_metadata(video_url):
    opts = {
        'quiet': True,
        'skip_download': True,
        'socket_timeout': SOCKET_TIMEOUT_SECONDS,
        'http_headers': {'User-Agent': COMMON_USER_AGENT}
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
    title = info.get('title')
    channel = info.get('uploader') or info.get('channel')
    return title, channel

# --- Core Processing ---

def process_video_details(video_url, do_audio, do_transcript):
    result = {
        'video_url': video_url,
        'title': None,
        'channel': None,
        'audio_download_url': None,
        'audio_server_path': None,
        'transcript_text': None,
        'transcript_language': None,
        'error': None
    }
    # Only YouTube supported
    if not ('youtube.com' in video_url or 'youtu.be' in video_url):
        result['error'] = 'Only YouTube URLs supported.'
        return result

    # Fetch metadata
    try:
        title, channel = fetch_metadata(video_url)
        result['title'] = title
        result['channel'] = channel
    except Exception as e:
        result['error'] = f"Metadata fetch error: {e}"
        return result

    # Audio extraction
    if do_audio:
        if not is_ffmpeg_available():
            result['error'] = 'FFmpeg not found.'
        else:
            try:
                ts = datetime.now().strftime('%Y-%m-%d_%H%M%S')
                safe = sanitize_filename(title)
                base = f"{ts}_{safe}"
                outdir = os.path.join(DOWNLOADS_BASE_DIR, base)
                os.makedirs(outdir, exist_ok=True)
                tmpl = os.path.join(outdir, f"{base}.%(ext)s")
                opts = {
                    'format': 'bestaudio/best',
                    'outtmpl': tmpl,
                    'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}],
                    'quiet': True,
                    'socket_timeout': SOCKET_TIMEOUT_SECONDS,
                    'http_headers': {'User-Agent': COMMON_USER_AGENT}
                }
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([video_url])
                mp3 = f"{base}.mp3"
                abspath = os.path.join(outdir, mp3)
                if os.path.exists(abspath):
                    result['audio_server_path'] = abspath
                    rel = f"{base}/{mp3}"
                    result['audio_download_url'] = url_for('serve_file', relative_path=rel, _external=True)
                else:
                    result['error'] = 'Audio file missing after download.'
            except Exception as e:
                result['error'] = f"Audio extract error: {e}"

    # Transcript extraction
    if do_transcript:
        temp = f"vtt_{uuid.uuid4().hex}"
        try:
            opts = {
                'writesubtitles': True,
                'writeautomaticsub': True,
                'subtitleslangs': ['en', 'ro'],
                'subtitlesformat': 'vtt',
                'skip_download': True,
                'outtmpl': os.path.join(TRANSCRIPTS_TEMP_DIR, temp),
                'quiet': True,
                'socket_timeout': SOCKET_TIMEOUT_SECONDS,
                'http_headers': {'User-Agent': COMMON_USER_AGENT}
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(video_url, download=True)
                subs = info.get('requested_subtitles') or {}
                path = None
                for lang in ['en', 'ro']:
                    sub_info = subs.get(lang)
                    if sub_info and sub_info.get('filepath'):
                        path = sub_info['filepath']
                        result['transcript_language'] = lang
                        break
            if not path:
                # Fallback scan
                for lang in ['en', 'ro']:
                    p = os.path.join(TRANSCRIPTS_TEMP_DIR, f"{temp}.{lang}.vtt")
                    if os.path.exists(p):
                        path = p
                        result['transcript_language'] = lang
                        break
            if path:
                with open(path, 'r', encoding='utf-8') as f:
                    text = vtt_to_plaintext(f.read())
                result['transcript_text'] = text
            else:
                result['error'] = result['error'] or 'No subtitles available.'
        except Exception as e:
            result['error'] = f"Transcript error: {e}"
        finally:
            # Cleanup
            for f in os.listdir(TRANSCRIPTS_TEMP_DIR):
                if f.startswith(temp):
                    try:
                        os.remove(os.path.join(TRANSCRIPTS_TEMP_DIR, f))
                    except:
                        pass

    return result

# --- Routes ---

@app.route('/api/process_video_details', methods=['GET'])
def api_process():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url parameter'}), 400
    get_audio = request.args.get('get_audio', 'true').lower() == 'true'
    get_transcript = request.args.get('get_transcript', 'false').lower() == 'true'
    data = process_video_details(url, get_audio, get_transcript)
    code = 200 if not data.get('error') else 500
    return jsonify(data), code

@app.route('/files/<path:relative_path>')
def serve_file(relative_path):
    return send_from_directory(DOWNLOADS_BASE_DIR, relative_path, as_attachment=True)

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy'}), 200
