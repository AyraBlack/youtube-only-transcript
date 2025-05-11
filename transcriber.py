import yt_dlp
import os
import shutil
import re # For VTT parsing and filename sanitization
from flask import Flask, request, jsonify, send_from_directory, url_for, Response # Added Response
import logging
import uuid # For unique temporary transcript file names
from datetime import datetime # For timestamped filenames

# --- Flask App Setup ---
app = Flask(__name__)
app.logger.setLevel(logging.INFO) # Set logging level for the application logger

# --- Directory Configuration ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_BASE_DIR = os.path.join(BASE_DIR, "api_downloads") # For MP3s
TRANSCRIPTS_TEMP_DIR = os.path.join(BASE_DIR, "api_transcripts_temp") # For temporary VTT files

if not os.path.exists(DOWNLOADS_BASE_DIR):
    os.makedirs(DOWNLOADS_BASE_DIR)
    app.logger.info(f"Created base MP3 downloads directory: {DOWNLOADS_BASE_DIR}")
if not os.path.exists(TRANSCRIPTS_TEMP_DIR):
    os.makedirs(TRANSCRIPTS_TEMP_DIR)
    app.logger.info(f"Created temporary transcripts directory: {TRANSCRIPTS_TEMP_DIR}")

# --- Constants ---
SOCKET_TIMEOUT_SECONDS = 180
COMMON_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"

# --- Read Proxy from Environment Variable ---
PROXY_URL_FROM_ENV = os.environ.get('PROXY_URL')
if PROXY_URL_FROM_ENV:
    proxy_display = PROXY_URL_FROM_ENV.split('@')[-1] if '@' in PROXY_URL_FROM_ENV else PROXY_URL_FROM_ENV
    app.logger.info(f"Using proxy from environment variable: {proxy_display}")
else:
    app.logger.info("PROXY_URL environment variable not set. Operating without proxy.")

def is_ffmpeg_available():
    """Checks if FFmpeg is installed and accessible."""
    return shutil.which("ffmpeg") is not None

def sanitize_filename(name_str, max_length=60): 
    """Sanitizes a string to be a safe filename component."""
    s = name_str.replace(' ', '_') 
    s = "".join(c if c.isalnum() or c in ('-', '_') else '_' for c in s)
    s = re.sub(r'_+', '_', s) 
    s = s.strip('_') 
    return s[:max_length]

def vtt_to_plaintext(vtt_content):
    """
    Converts VTT subtitle content to plain text, robustly handling metadata and duplicates.
    """
    app.logger.debug(f"VTT content for parsing (first 500 chars):\n{vtt_content[:500]}...")
    
    lines = vtt_content.splitlines()
    collected_text_segments = [] # Stores processed text for each cue/segment
    current_cue_lines = [] # Temporarily stores lines for the current cue
    in_cue_text_block = False # Flag to indicate we are processing lines of a cue's text

    for line in lines:
        line_stripped = line.strip()

        # Handle end of a cue block (empty line)
        if not line_stripped:
            if in_cue_text_block and current_cue_lines:
                # Join lines of the current cue, clean, and add to segments
                segment_text = " ".join(current_cue_lines).strip()
                if segment_text: # Only add if there's actual text
                    collected_text_segments.append(segment_text)
                current_cue_lines = [] # Reset for next cue
            in_cue_text_block = False
            continue

        # Skip common VTT headers and metadata lines explicitly
        if line_stripped == "WEBVTT" or \
           line_stripped.lower().startswith("kind:") or \
           line_stripped.lower().startswith("language:") or \
           line_stripped.lower().startswith("style") or \
           line_stripped.lower().startswith("note") or \
           line_stripped.lower().startswith("region"):
            in_cue_text_block = False # These lines are not part of cue text
            continue

        # Timestamp lines indicate the start of a new cue's text block
        if "-->" in line_stripped:
            if current_cue_lines: # Process any text from the *previous* cue
                segment_text = " ".join(current_cue_lines).strip()
                if segment_text:
                    collected_text_segments.append(segment_text)
            current_cue_lines = [] # Reset for the new cue
            in_cue_text_block = True # Next non-empty lines are part of this cue's text
            continue

        # Skip cue numbers if they appear on their own line and we are not already in a text block
        if line_stripped.isdigit() and not in_cue_text_block:
            continue
            
        if in_cue_text_block:
            # Remove VTT specific tags like <v Author>, <i>, <b>, <c.xxxxxx>, and other HTML-like tags
            cleaned_line = re.sub(r'<c\.[^>]+>', '', line_stripped) 
            cleaned_line = re.sub(r'</c>', '', cleaned_line)      
            cleaned_line = re.sub(r'<v[^>]*>', '', cleaned_line)    
            cleaned_line = re.sub(r'</v>', '', cleaned_line)       
            cleaned_line = re.sub(r'<i>', '', cleaned_line)        
            cleaned_line = re.sub(r'</i>', '', cleaned_line)       
            cleaned_line = re.sub(r'<b>', '', cleaned_line)        
            cleaned_line = re.sub(r'</b>', '', cleaned_line)       
            cleaned_line = re.sub(r'<[^>]+>', '', cleaned_line) # Generic tag removal
            
            cleaned_line = cleaned_line.replace('&nbsp;', ' ').replace(' ', ' ') 
            cleaned_line = cleaned_line.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
            
            if cleaned_line.strip(): # Only add if there's actual text
                current_cue_lines.append(cleaned_line.strip())
    
    # Add any text from the very last cue
    if current_cue_lines:
        segment_text = " ".join(current_cue_lines).strip()
        if segment_text:
            collected_text_segments.append(segment_text)

    if not collected_text_segments:
        app.logger.warning("No text segments extracted from VTT content after initial parsing.")
        return ""

    # Deduplicate consecutive identical segments
    deduplicated_final_lines = []
    if collected_text_segments:
        deduplicated_final_lines.append(collected_text_segments[0])
        for i in range(1, len(collected_text_segments)):
            if collected_text_segments[i] != deduplicated_final_lines[-1]: # Compare full segments
                deduplicated_final_lines.append(collected_text_segments[i])
            # else:
                # app.logger.debug(f"Skipping duplicate segment: '{collected_text_segments[i]}'")
                
    final_text = "\n".join(deduplicated_final_lines)
    app.logger.info(f"Successfully parsed VTT to plain text. Output length: {len(final_text)}")
    app.logger.debug(f"Parsed plain text (first 500 chars):\n{final_text[:500]}...")
    return final_text

def _get_common_ydl_opts(include_logger=True):
    opts = {
        'socket_timeout': SOCKET_TIMEOUT_SECONDS,
        'http_headers': {'User-Agent': COMMON_USER_AGENT},
        'noplaylist': True,
        'verbose': False,
    }
    if include_logger:
        opts['logger'] = app.logger
    if PROXY_URL_FROM_ENV:
        opts['proxy'] = PROXY_URL_FROM_ENV
    return opts

def process_video_details(video_url, perform_audio_extraction=True, perform_transcript_extraction=False, audio_format="mp3"):
    app.logger.info(f"Processing video details for URL: {video_url}, get_audio: {perform_audio_extraction}, get_transcript: {perform_transcript_extraction}")
    
    response = {
        "video_url_processed": video_url,
        "video_title": None,
        "author": None, 
        "audio_download_url": None,
        "audio_server_path": None,
        "transcript_text": None,
        "transcript_language_detected": None,
        "error": None
    }

    try:
        common_opts_info = _get_common_ydl_opts()
        info_opts = {
            **common_opts_info,
            'quiet': True, 
            'extract_flat': 'in_playlist', 
        }
        with yt_dlp.YoutubeDL(info_opts) as ydl_info:
            app.logger.info(f"Fetching initial metadata for: {video_url}...")
            info_dict = ydl_info.extract_info(video_url, download=False)
            
            response["video_title"] = info_dict.get('title', f'unknown_title_{uuid.uuid4().hex[:6]}')
            response["author"] = info_dict.get('uploader', info_dict.get('channel', f'unknown_author_{uuid.uuid4().hex[:6]}'))
            app.logger.info(f"Metadata fetched - Title: '{response['video_title']}', Author: '{response['author']}'")

    except yt_dlp.utils.DownloadError as de:
        app.logger.error(f"yt-dlp DownloadError during initial metadata fetch for {video_url}: {de}")
        response["error"] = f"Failed to fetch initial video metadata: {str(de)}"
        return response 
    except Exception as e:
        app.logger.error(f"Unexpected error during initial metadata fetch for {video_url}: {e}", exc_info=True)
        response["error"] = f"Unexpected error fetching initial video metadata: {str(e)}"
        return response

    if perform_audio_extraction:
        if not is_ffmpeg_available():
            audio_error_msg = "FFmpeg not found, cannot extract audio."
            app.logger.error(audio_error_msg)
            if not response["error"]: response["error"] = audio_error_msg
        else:
            try:
                current_time_str = datetime.now().strftime("%Y-%m-%d_%H%M%S")
                sanitized_title_part = sanitize_filename(response["video_title"] if response["video_title"] else "untitled")
                base_output_filename_safe = f"{current_time_str}_{sanitized_title_part}"
                
                request_folder_name = base_output_filename_safe
                request_download_dir_abs = os.path.join(DOWNLOADS_BASE_DIR, request_folder_name)
                if not os.path.exists(request_download_dir_abs):
                    os.makedirs(request_download_dir_abs)

                actual_disk_filename_template = f'{base_output_filename_safe}.%(ext)s'
                output_template_audio_abs = os.path.join(request_download_dir_abs, actual_disk_filename_template)

                common_opts_audio = _get_common_ydl_opts()
                ydl_opts_audio = {
                    **common_opts_audio,
                    'format': 'bestaudio/best',
                    'outtmpl': output_template_audio_abs,
                    'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': audio_format}],
                    'quiet': False, 'noprogress': False, 
                }
                with yt_dlp.YoutubeDL(ydl_opts_audio) as ydl_audio:
                    app.logger.info(f"Starting audio download/extraction for {video_url}...")
                    error_code = ydl_audio.download([video_url])
                    if error_code != 0:
                        audio_error = f"yt-dlp audio process failed (code {error_code})."
                        app.logger.error(audio_error)
                        if not response["error"]: response["error"] = audio_error 
                    else:
                        final_audio_filename_on_disk = f"{base_output_filename_safe}.{audio_format}"
                        response["audio_server_path"] = os.path.join(request_download_dir_abs, final_audio_filename_on_disk)
                        response["audio_relative_path"] = os.path.join(request_folder_name, final_audio_filename_on_disk)
                        if os.path.exists(response["audio_server_path"]):
                            app.logger.info(f"Audio extracted: {response['audio_server_path']}")
                            response["audio_download_url"] = url_for('serve_downloaded_file', 
                                                                    relative_file_path=response["audio_relative_path"], 
                                                                    _external=True)
                        else:
                            audio_error = f"Audio file not found post-processing at {response['audio_server_path']}."
                            app.logger.error(audio_error)
                            if not response["error"]: response["error"] = audio_error
                            response["audio_server_path"] = None 
                            response["audio_relative_path"] = None
            except Exception as e_audio:
                audio_error = f"Unexpected error during audio extraction: {str(e_audio)}"
                app.logger.error(f"Error in audio extraction: {e_audio}", exc_info=True)
                if not response["error"]: response["error"] = audio_error
    
    is_youtube_url = "youtube.com/" in video_url if video_url else False 
    if perform_transcript_extraction and is_youtube_url:
        temp_vtt_basename = f"transcript_{uuid.uuid4().hex}"
        temp_vtt_dir = TRANSCRIPTS_TEMP_DIR
        output_template_transcript_abs = os.path.join(temp_vtt_dir, temp_vtt_basename)
        
        common_opts_transcript = _get_common_ydl_opts()
        ydl_opts_transcript = {
            **common_opts_transcript,
            'writesubtitles': True, 'writeautomaticsub': True,
            'subtitleslangs': ['en', 'ro'], # Try English first, then Romanian
            'subtitlesformat': 'vtt', 'skip_download': True,
            'outtmpl': output_template_transcript_abs,
            'quiet': False, 'noprogress': False,
        }
        downloaded_vtt_path = None
        try:
            with yt_dlp.YoutubeDL(ydl_opts_transcript) as ydl_transcript:
                app.logger.info(f"Starting transcript download for {video_url} (langs: en, ro)...")
                info_dict_subs = ydl_transcript.extract_info(video_url, download=True)
                
                requested_subs = info_dict_subs.get('requested_subtitles')
                if requested_subs:
                    for lang_code in ['en', 'ro']: 
                        if lang_code in requested_subs:
                            sub_info = requested_subs[lang_code]
                            if sub_info.get('filepath') and os.path.exists(sub_info['filepath']):
                                downloaded_vtt_path = sub_info['filepath']
                                response["transcript_language_detected"] = lang_code
                                app.logger.info(f"Transcript VTT downloaded: {downloaded_vtt_path} (Lang: {lang_code})")
                                break
                if not downloaded_vtt_path: 
                    app.logger.info("Transcript path not in 'requested_subtitles', scanning directory...")
                    for lang in ['en', 'ro']:
                        potential_path = os.path.join(temp_vtt_dir, f"{temp_vtt_basename}.{lang}.vtt")
                        if os.path.exists(potential_path):
                            downloaded_vtt_path = potential_path
                            response["transcript_language_detected"] = lang
                            app.logger.info(f"Transcript VTT found by scan: {downloaded_vtt_path} (Lang: {lang})")
                            break
                
                if downloaded_vtt_path:
                    with open(downloaded_vtt_path, 'r', encoding='utf-8') as f_vtt:
                        vtt_content = f_vtt.read()
                    response["transcript_text"] = vtt_to_plaintext(vtt_content) 
                    app.logger.info(f"Transcript parsed for language: {response['transcript_language_detected']}")
                else:
                    transcript_error = "Transcript VTT not found or not available in EN/RO."
                    app.logger.warning(transcript_error + f" for {video_url}")
                    if not response["error"]: response["error"] = transcript_error 
        
        except yt_dlp.utils.DownloadError as de_subs:
            transcript_error = f"yt-dlp DownloadError during transcript fetch: {str(de_subs)}"
            app.logger.error(transcript_error + f" for {video_url}")
            if not response["error"]: response["error"] = transcript_error
        except Exception as e_subs:
            transcript_error = f"Unexpected error during transcript processing: {str(e_subs)}"
            app.logger.error(f"Error in transcript extraction: {e_subs}", exc_info=True)
            if not response["error"]: response["error"] = transcript_error
        finally:
            if downloaded_vtt_path and os.path.exists(downloaded_vtt_path):
                if os.path.exists(downloaded_vtt_path): 
                    try:
                        os.remove(downloaded_vtt_path)
                        app.logger.info(f"Deleted temporary transcript file: {downloaded_vtt_path}")
                    except Exception as e_del:
                        app.logger.error(f"Error deleting temporary VTT file {downloaded_vtt_path}: {e_del}")

    elif perform_transcript_extraction and not is_youtube_url:
        app.logger.info(f"Transcript extraction skipped for non-YouTube URL: {video_url}")
        response["transcript_text"] = "Transcript extraction currently only supported for YouTube URLs by this endpoint."

    return response

@app.route('/api/process_video_details', methods=['GET'])
def api_process_video_details_route():
    app.logger.info("Received request for /api/process_video_details")
    video_url_param = request.args.get('url')
    
    is_youtube = "youtube.com/" in video_url_param if video_url_param else False
    
    get_audio_str = request.args.get('get_audio', 'true').lower()
    default_get_transcript = 'true' if is_youtube else 'false'
    get_transcript_str = request.args.get('get_transcript', default_get_transcript).lower()

    if not video_url_param:
        app.logger.warning("Missing 'url' parameter in request.")
        return jsonify({"error": "Missing 'url' parameter"}), 400

    perform_audio = get_audio_str == 'true'
    perform_transcript = get_transcript_str == 'true'
    
    app.logger.info(f"Processing URL: {video_url_param}, Get Audio: {perform_audio}, Get Transcript: {perform_transcript}")
    
    result = process_video_details(video_url_param, 
                                   perform_audio_extraction=perform_audio, 
                                   perform_transcript_extraction=perform_transcript)

    # If only transcript was requested and it was successful, return plain text
    if perform_transcript and not perform_audio and result.get("transcript_text") and not result.get("error"):
        app.logger.info("Returning plain text transcript directly.")
        return Response(result["transcript_text"], mimetype='text/plain; charset=utf-8')
    
    # Otherwise, return JSON
    critical_error_occured = result.get("error") and not (result.get("audio_download_url") or result.get("transcript_text"))
    status_code = 500 if critical_error_occured else 200
    
    return jsonify(result), status_code

@app.route('/files/<path:relative_file_path>')
def serve_downloaded_file(relative_file_path):
    app.logger.info(f"Request to serve file. Base directory: '{DOWNLOADS_BASE_DIR}', Relative path from URL: '{relative_file_path}'")
    try:
        return send_from_directory(DOWNLOADS_BASE_DIR, relative_file_path, as_attachment=True)
    except FileNotFoundError:
        app.logger.error(f"FileNotFoundError: File not found for serving. Checked path: '{os.path.join(DOWNLOADS_BASE_DIR, relative_file_path)}'")
        return jsonify({"error": "File not found. It may have been moved, deleted, or the path is incorrect after processing."}), 404
    except Exception as e:
        app.logger.error(f"Error serving file '{relative_file_path}': {type(e).__name__} - {str(e)}", exc_info=True)
        return jsonify({"error": "Could not serve file due to an internal issue."}), 500

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"}), 200

if __name__ == '__main__':
    app.logger.info("--- Starting Consolidated Video Processing API (Flask Development Server) ---")
    if PROXY_URL_FROM_ENV:
        app.logger.info(f"Local run would use proxy: {PROXY_URL_FROM_ENV.split('@')[1] if '@' in PROXY_URL_FROM_ENV else 'Proxy configured'}")
    if not is_ffmpeg_available():
        app.logger.critical("CRITICAL: FFmpeg is not installed or not found. This API requires FFmpeg.")
    else:
        app.logger.info("FFmpeg found (local check).")
    app.logger.info(f"MP3s will be saved under: {DOWNLOADS_BASE_DIR}")
    app.logger.info(f"Temp transcripts under: {TRANSCRIPTS_TEMP_DIR}")
    app.run(host='0.0.0.0', port=5001, debug=True)
