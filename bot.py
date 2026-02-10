import os
import sys
import re
import requests
import json
import uuid
import math
import threading
import time
import urllib.parse
from flask import Flask, render_template_string, request, redirect, url_for, Response, jsonify, abort
from pymongo import MongoClient
from bson.objectid import ObjectId
from dotenv import load_dotenv
from datetime import datetime

# --- কনফিগারেশন লোড ---
load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(24)

# --- ভেরিয়েবলসমূহ ---
MONGO_URI = os.getenv("MONGO_URI")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME")
PUBLIC_CHANNEL_ID = os.getenv("PUBLIC_CHANNEL_ID")
SOURCE_CHANNEL_ID = os.getenv("SOURCE_CHANNEL_ID") # রিপোর্ট এখানে আসবে
WEBSITE_URL = os.getenv("WEBSITE_URL")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# আপনার টেলিগ্রাম অ্যাডমিন ইউজারনেম (রিকোয়েস্ট বাটন এর জন্য)
# এটি পরিবর্তন করে আপনার ইউজারনেম দিন (যেমন: https://t.me/RahimAdmin)
ADMIN_CONTACT_URL = "https://t.me/CineZoneBDBot" 

# অটো ডিলিট সময় (সেকেন্ডে) - ১০ মিনিট
DELETE_TIMEOUT = 600 

# নোটিফিকেশন কুলডাউন (সেকেন্ডে) - ৩০ মিনিট
NOTIFICATION_COOLDOWN = 1800 

# এডমিন ক্রেডেনশিয়াল
ADMIN_USER = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASSWORD", "admin")

# --- ডেটাবেস কানেকশন ---
try:
    client = MongoClient(MONGO_URI)
    db = client["moviezone_db"]
    movies = db["movies"]
    settings = db["settings"]
    categories = db["categories"] 
    print("✅ MongoDB Connected Successfully!")
except Exception as e:
    print(f"❌ MongoDB Connection Error: {e}")
    sys.exit(1)

# === Helper Functions ===

def clean_filename(filename):
    """ ফাইলের নাম ক্লিন করে মেইন টাইটেল বের করে। """
    name = os.path.splitext(filename)[0]
    name = re.sub(r'[._\-\+\[\]\(\)]', ' ', name)
    stop_pattern = r'(\b(19|20)\d{2}\b|\bS\d+|\bSeason|\bEp?\s*\d+|\b480p|\b720p|\b1080p|\b2160p|\bHD|\bWeb-?dl|\bBluray|\bDual|\bHindi|\bBangla)'
    match = re.search(stop_pattern, name, re.IGNORECASE)
    if match:
        name = name[:match.start()]
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def get_file_quality(filename):
    filename = filename.lower()
    if "4k" in filename or "2160p" in filename: return "4K UHD"
    if "1080p" in filename: return "1080p FHD"
    if "720p" in filename: return "720p HD"
    if "480p" in filename: return "480p SD"
    return "HD"

def detect_language(text):
    text = text.lower()
    detected = []
    if re.search(r'\b(multi|multi audio)\b', text): return "Multi Audio"
    if re.search(r'\b(dual|dual audio)\b', text): detected.append("Dual Audio")

    lang_map = {
        'Bengali': ['bengali', 'bangla', 'ben'],
        'Hindi': ['hindi', 'hin'],
        'English': ['english', 'eng'],
        'Tamil': ['tamil', 'tam'],
        'Telugu': ['telugu', 'tel'],
        'Korean': ['korean', 'kor'],
        'Japanese': ['japanese', 'jap']
    }
    for lang_name, keywords in lang_map.items():
        pattern = r'\b(' + '|'.join(keywords) + r')\b'
        if re.search(pattern, text):
            detected.append(lang_name)

    if not detected: return "English"
    return " + ".join(list(dict.fromkeys(detected)))

def get_episode_label(filename):
    label = ""
    season = ""
    match_s = re.search(r'\b(S|Season)\s*(\d+)', filename, re.IGNORECASE)
    if match_s: season = f"S{int(match_s.group(2)):02d}"

    match_range = re.search(r'E(\d+)\s*-\s*E?(\d+)', filename, re.IGNORECASE)
    if match_range:
        start, end = int(match_range.group(1)), int(match_range.group(2))
        episode_part = f"E{start:02d}-{end:02d}"
        return f"{season} {episode_part}" if season else episode_part

    match_se = re.search(r'\bS(\d+)\s*E(\d+)\b', filename, re.IGNORECASE)
    if match_se: return f"S{int(match_se.group(1)):02d} E{int(match_se.group(2)):02d}"
    
    match_ep = re.search(r'\b(Episode|Ep|E)\s*(\d+)\b', filename, re.IGNORECASE)
    if match_ep:
        ep_num = int(match_ep.group(2))
        if ep_num < 1900: return f"{season} Episode {ep_num}".strip()
    
    if season: return f"Season {int(match_s.group(2))}"
    return None

def is_adult_content(title, genres=[]):
    """ টাইটেল এবং কিওয়ার্ড চেক করে ১৮+ ডিটেক্ট করে """
    adult_keywords = ['18+', 'adult', 'uncut', 'erotic', 'hot', 'sex', 'nude', 'romance', 'thriller', 'porn', 'xxx']
    for word in adult_keywords:
        if re.search(r'\b' + word + r'\b', title, re.IGNORECASE):
            return True
    return False

# --- YouTube ID Extractor ---
def extract_youtube_id(url):
    if not url: return None
    regex = r'(?:youtube\.com\/(?:[^\/]+\/.+\/|(?:v|e(?:mbed)?)\/|.*[?&]v=)|youtu\.be\/)([^"&?\/\s]{11})'
    match = re.search(regex, url)
    return match.group(1) if match else None

# --- BACKGROUND DELETE FUNCTION ---
def delete_message_later(chat_id, message_id, delay):
    time.sleep(delay)
    try:
        del_url = f"{TELEGRAM_API_URL}/deleteMessage"
        requests.post(del_url, json={"chat_id": chat_id, "message_id": message_id})
    except Exception as e:
        print(f"⚠️ Failed to delete message: {e}")

# --- AUTO IMPORT & SCHEDULER (NEW UPDATE) ---
def auto_import_movies():
    """ TMDB থেকে অটোমেটিক নতুন এবং ট্রেন্ডিং মুভি ফেচ করে ডেটাবেসে সেভ করবে """
    if not TMDB_API_KEY:
        print("⚠️ TMDB API Key Missing. Auto-import skipped.")
        return

    print("🔄 Auto-Import Started: Fetching Trending & Now Playing...")
    
    # ২ ধরনের লিস্ট আনা হবে: ১. বর্তমানে চলছে (Now Playing), ২. ট্রেন্ডিং (Trending)
    api_urls = [
        f"https://api.themoviedb.org/3/movie/now_playing?api_key={TMDB_API_KEY}&language=en-US&page=1",
        f"https://api.themoviedb.org/3/trending/movie/day?api_key={TMDB_API_KEY}"
    ]

    count = 0
    now_utc = datetime.now(datetime.UTC) if hasattr(datetime, 'UTC') else datetime.utcnow()

    for url in api_urls:
        try:
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                for item in data.get('results', []):
                    title = item.get('title')
                    # যদি টাইটেল না থাকে বা ডেটাবেসে অলরেডি থাকে, তাহলে স্কিপ করবে
                    if not title or movies.find_one({"title": title}):
                        continue
                    
                    # মুভি ডিটেইলস সাজানো
                    new_movie = {
                        "tmdb_id": item.get("id"),
                        "title": title,
                        "overview": item.get("overview"),
                        "poster": f"https://image.tmdb.org/t/p/w500{item.get('poster_path')}" if item.get('poster_path') else None,
                        "backdrop": f"https://image.tmdb.org/t/p/w1280{item.get('backdrop_path')}" if item.get('backdrop_path') else None,
                        "release_date": item.get("release_date"),
                        "vote_average": item.get("vote_average"),
                        "genres": [], # জেনরা পরে এডিট করা যাবে
                        "language": "English", # ডিফল্ট
                        "type": "movie",
                        "category": "Uncategorized",
                        "is_adult": item.get("adult", False),
                        "files": [], # শুরুতে কোনো ফাইল থাকবে না (Request Button দেখাবে)
                        "created_at": now_utc,
                        "updated_at": now_utc
                    }
                    
                    movies.insert_one(new_movie)
                    count += 1
        except Exception as e:
            print(f"❌ Auto-Import Error: {e}")
            
    if count > 0:
        print(f"✅ Auto-Import Finished! Added {count} new movies.")
    else:
        print("✅ Auto-Import Checked: No new movies found.")

def start_scheduler():
    """ প্রতি ৬ ঘণ্টা পর পর অটোমেটিক মুভি চেক করবে """
    while True:
        try:
            auto_import_movies()
        except Exception as e:
            print(f"Scheduler Error: {e}")
        time.sleep(21600) # 21600 সেকেন্ড = ৬ ঘণ্টা

# --- TMDB FUNCTION ---
def get_tmdb_details(title, content_type="movie", year=None):
    if not TMDB_API_KEY: return {"title": title}
    tmdb_type = "tv" if content_type == "series" else "movie"
    try:
        query_str = requests.utils.quote(title)
        search_url = f"https://api.themoviedb.org/3/search/{tmdb_type}?api_key={TMDB_API_KEY}&query={query_str}"
        if year and tmdb_type == "movie":
            search_url += f"&year={year}"

        data = requests.get(search_url, timeout=5).json()
        if data.get("results"):
            res = data["results"][0]
            m_id = res.get("id")
            
            details_url = f"https://api.themoviedb.org/3/{tmdb_type}/{m_id}?api_key={TMDB_API_KEY}&append_to_response=credits,videos"
            extra = requests.get(details_url, timeout=5).json()

            trailer_key = None
            if extra.get('videos', {}).get('results'):
                for vid in extra['videos']['results']:
                    if vid['type'] == 'Trailer' and vid['site'] == 'YouTube':
                        trailer_key = vid['key']
                        break
            
            cast_list = []
            if extra.get('credits', {}).get('cast'):
                for actor in extra['credits']['cast'][:6]:
                    cast_list.append({
                        'name': actor['name'],
                        'img': f"https://image.tmdb.org/t/p/w185{actor['profile_path']}" if actor.get('profile_path') else None
                    })

            genres = [g['name'] for g in extra.get('genres', [])]
            runtime = extra.get("runtime") or (extra.get("episode_run_time")[0] if extra.get("episode_run_time") else None)

            poster = f"https://image.tmdb.org/t/p/w500{res['poster_path']}" if res.get('poster_path') else None
            backdrop = f"https://image.tmdb.org/t/p/w1280{res['backdrop_path']}" if res.get('backdrop_path') else None
            is_adult_tmdb = res.get("adult", False)

            return {
                "tmdb_id": res.get("id"),
                "title": res.get("name") if tmdb_type == "tv" else res.get("title"),
                "overview": res.get("overview"),
                "poster": poster,
                "backdrop": backdrop,
                "release_date": res.get("first_air_date") if tmdb_type == "tv" else res.get("release_date"),
                "vote_average": res.get("vote_average"),
                "genres": genres,        
                "runtime": runtime, 
                "trailer": trailer_key,  
                "cast": cast_list,
                "adult": is_adult_tmdb
            }
    except Exception as e:
        print(f"TMDB Error: {e}")
    return {"title": title}

def escape_markdown(text):
    if not text: return ""
    chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(chars)}])', r'\\\1', text)

def check_auth():
    auth = request.authorization
    if not auth or not (auth.username == ADMIN_USER and auth.password == ADMIN_PASS):
        return False
    return True

@app.context_processor
def inject_globals():
    ad_codes = settings.find_one() or {}
    return dict(
        ad_settings=ad_codes, 
        BOT_USERNAME=BOT_USERNAME, 
        site_name="MovieZone",
        quote=urllib.parse.quote 
    )

# --- ANTI-BAN: CRAWLER BLOCKER ---
@app.before_request
def block_bots():
    # পরিচিত ক্রলার এবং কপিরাইট বট ব্লক করা হচ্ছে
    user_agent = request.headers.get('User-Agent', '').lower()
    blocked_bots = ['googlebot', 'bingbot', 'ahrefsbot', 'semrushbot', 'mj12bot', 'dotbot', 'petalbot', 'bytespider', 'dmca', 'copyright', 'monitor', 'internet-archive']
    
    if any(bot in user_agent for bot in blocked_bots):
        # বটদের 404 পেজ দেখানো হবে
        abort(404)

# --- ROBOTS.TXT (Stop Indexing) ---
@app.route('/robots.txt')
def robots_txt():
    return Response("User-agent: *\nDisallow: /", mimetype="text/plain")


# === TELEGRAM WEBHOOK ===
@app.route(f'/webhook/{BOT_TOKEN}', methods=['POST'])
def telegram_webhook():
    update = request.get_json()
    if not update: return jsonify({'status': 'ignored'})

    MY_CHANNEL_LINK = "https://t.me/TGLinkBase" 

    if 'channel_post' in update:
        msg = update['channel_post']
        chat_id = str(msg.get('chat', {}).get('id'))
        
        if SOURCE_CHANNEL_ID and chat_id != str(SOURCE_CHANNEL_ID):
            return jsonify({'status': 'wrong_channel'})

        file_id = None
        file_name = "Unknown"
        file_size_mb = 0
        file_type = "document"

        if 'video' in msg:
            video = msg['video']
            file_id = video['file_id']
            file_name = video.get('file_name', msg.get('caption', 'Unknown Video'))
            file_size_mb = video.get('file_size', 0) / (1024 * 1024)
            file_type = "video"
        elif 'document' in msg:
            doc = msg['document']
            file_id = doc['file_id']
            file_name = doc.get('file_name', 'Unknown Document')
            file_size_mb = doc.get('file_size', 0) / (1024 * 1024)
            file_type = "document"

        if not file_id: return jsonify({'status': 'no_file'})

        raw_caption = msg.get('caption')
        raw_input = raw_caption if raw_caption else file_name
        
        search_title = clean_filename(raw_input) 
        year_match = re.search(r'\b(19|20)\d{2}\b', raw_input)
        search_year = year_match.group(0) if year_match else None
        
        content_type = "movie"
        if re.search(r'(S\d+|Season|Episode|Ep\s*\d+|Combined|E\d+-E\d+)', file_name, re.IGNORECASE) or re.search(r'(S\d+|Season)', str(raw_caption), re.IGNORECASE):
            content_type = "series"

        tmdb_data = get_tmdb_details(search_title, content_type, search_year)
        final_title = tmdb_data.get('title', search_title)
        quality = get_file_quality(file_name)
        
        is_adult = tmdb_data.get('adult', False)
        if not is_adult:
            is_adult = is_adult_content(final_title)

        episode_label = get_episode_label(file_name)
        if content_type == "series" and not episode_label:
            clean_part = file_name.replace(search_title, "").replace(".", " ").strip()
            if len(clean_part) > 3:
                episode_label = clean_part[:25]

        language = detect_language(raw_input)
        unique_code = str(uuid.uuid4())[:8]

        # Use timezone-aware UTC
        current_time = datetime.now(datetime.UTC) if hasattr(datetime, 'UTC') else datetime.utcnow()

        file_obj = {
            "file_id": file_id,
            "unique_code": unique_code,
            "filename": file_name,
            "quality": quality,
            "episode_label": episode_label,
            "size": f"{file_size_mb:.2f} MB",
            "file_type": file_type,
            "added_at": current_time
        }

        # ডেটাবেস চেক: মুভি আগে থেকেই আছে কি না (Auto Import বা আগের আপলোড)
        existing_movie = movies.find_one({"title": final_title})
        movie_id = None
        should_notify = False

        if existing_movie:
            is_duplicate = False
            for f in existing_movie.get('files', []):
                if f.get('file_id') == file_id:
                    is_duplicate = True
                    break
            if not is_duplicate:
                movies.update_one(
                    {"_id": existing_movie['_id']},
                    {"$push": {"files": file_obj}, "$set": {"updated_at": current_time}}
                )
                movie_id = existing_movie['_id']
                should_notify = True
        else:
            should_notify = True
            new_movie = {
                "title": final_title,
                "overview": tmdb_data.get('overview'),
                "poster": tmdb_data.get('poster'),
                "backdrop": tmdb_data.get('backdrop'),
                "release_date": tmdb_data.get('release_date'),
                "vote_average": tmdb_data.get('vote_average'),
                "genres": tmdb_data.get('genres'),
                "runtime": tmdb_data.get('runtime'),
                "trailer": tmdb_data.get('trailer'),
                "cast": tmdb_data.get('cast'),
                "language": language,
                "type": content_type,
                "category": "Uncategorized",
                "is_adult": is_adult,
                "files": [file_obj],
                "created_at": current_time,
                "updated_at": current_time
            }
            res = movies.insert_one(new_movie)
            movie_id = res.inserted_id

        if movie_id and WEBSITE_URL:
            direct_link = f"{WEBSITE_URL.rstrip('/')}/movie/{str(movie_id)}"
            home_link = WEBSITE_URL.rstrip('/')
            
            edit_payload = {
                'chat_id': chat_id,
                'message_id': msg['message_id'],
                'reply_markup': json.dumps({
                    "inline_keyboard": [[{"text": "▶️ Check on Website", "url": direct_link}]]
                })
            }
            try: requests.post(f"{TELEGRAM_API_URL}/editMessageReplyMarkup", json=edit_payload)
            except: pass

            current_movie = movies.find_one({"_id": movie_id})
            last_notified = current_movie.get("last_notified")
            
            is_spamming = False
            if last_notified:
                now = datetime.now(datetime.UTC) if hasattr(datetime, 'UTC') else datetime.utcnow()
                if hasattr(datetime, 'UTC') and last_notified.tzinfo is None:
                    last_notified = last_notified.replace(tzinfo=datetime.UTC)
                
                time_diff = (now - last_notified).total_seconds()
                if time_diff < NOTIFICATION_COOLDOWN:
                    is_spamming = True

            if PUBLIC_CHANNEL_ID and should_notify and tmdb_data.get('poster') and not is_spamming:
                notify_caption = f"🎬 *{escape_markdown(final_title)}*\n"
                if episode_label: notify_caption += f"📌 {escape_markdown(episode_label)}\n"
                
                notify_caption += f"\n⭐ Rating: {tmdb_data.get('vote_average', 'N/A')}\n"
                notify_caption += f"📅 Year: {(tmdb_data.get('release_date') or 'N/A')[:4]}\n"
                notify_caption += f"🔊 Language: {language}\n"
                notify_caption += f"💿 Quality: {quality}\n"
                notify_caption += f"📦 Size: {file_size_mb:.2f} MB\n\n"
                notify_caption += f"🔗 *Download Now:* [Click Here]({home_link})"

                pub_keyboard = [
                    [{"text": "📥 Download / Watch Online", "url": home_link}],
                    [{"text": "📢 Join Our Channel", "url": MY_CHANNEL_LINK}]
                ]

                notify_payload = {
                    'chat_id': PUBLIC_CHANNEL_ID,
                    'parse_mode': 'Markdown',
                    'reply_markup': json.dumps({"inline_keyboard": pub_keyboard}),
                    'photo': tmdb_data.get('poster'),
                    'caption': notify_caption
                }

                try: 
                    resp = requests.post(f"{TELEGRAM_API_URL}/sendPhoto", json=notify_payload)
                    if resp.json().get('ok'):
                        now_utc = datetime.now(datetime.UTC) if hasattr(datetime, 'UTC') else datetime.utcnow()
                        movies.update_one({"_id": movie_id}, {"$set": {"last_notified": now_utc}})
                except: pass

        return jsonify({'status': 'success'})

    elif 'message' in update:
        msg = update['message']
        chat_id = msg.get('chat', {}).get('id')
        text = msg.get('text', '')

        if text.startswith('/start'):
            parts = text.split()
            if len(parts) > 1:
                code = parts[1]
                movie = movies.find_one({"files.unique_code": code})
                if movie:
                    target_file = next((f for f in movie['files'] if f['unique_code'] == code), None)
                    if target_file:
                        caption = f"🎬 *{escape_markdown(movie['title'])}*\n"
                        if target_file.get('episode_label'):
                            caption += f"📌 {escape_markdown(target_file['episode_label'])}\n"
                        caption += f"💿 Quality: {target_file['quality']}\n"
                        caption += f"📦 Size: {target_file['size']}\n\n"
                        caption += f"⚠️ *File will be deleted in 10 minutes! Forward it now!*"
                        
                        file_keyboard = {
                            "inline_keyboard": [
                                [{"text": "📢 Join Update Channel", "url": MY_CHANNEL_LINK}]
                            ]
                        }

                        payload = {
                            'chat_id': chat_id, 
                            'caption': caption, 
                            'parse_mode': 'Markdown',
                            'reply_markup': json.dumps(file_keyboard)
                        }
                        
                        method = 'sendVideo' if target_file['file_type'] == 'video' else 'sendDocument'
                        if target_file['file_type'] == 'video': payload['video'] = target_file['file_id']
                        else: payload['document'] = target_file['file_id']
                        
                        try:
                            response = requests.post(f"{TELEGRAM_API_URL}/{method}", json=payload)
                            resp_data = response.json()
                            
                            if resp_data.get('ok'):
                                sent_msg_id = resp_data['result']['message_id']
                                threading.Thread(target=delete_message_later, args=(chat_id, sent_msg_id, DELETE_TIMEOUT)).start()
                        except Exception as e:
                            print(f"Error sending file: {e}")
                    else:
                        requests.post(f"{TELEGRAM_API_URL}/sendMessage", json={'chat_id': chat_id, 'text': "❌ File expired or removed."})
                else:
                    requests.post(f"{TELEGRAM_API_URL}/sendMessage", json={'chat_id': chat_id, 'text': "❌ Invalid Link."})
            else:
                welcome_kb = {
                    "inline_keyboard": [[{"text": "📢 Join Our Channel", "url": MY_CHANNEL_LINK}]]
                }
                requests.post(f"{TELEGRAM_API_URL}/sendMessage", json={
                    'chat_id': chat_id, 
                    'text': "👋 Welcome! Use the website to download movies.",
                    'reply_markup': json.dumps(welcome_kb)
                })

    return jsonify({'status': 'ok'})

# ================================
#        FRONTEND TEMPLATES
# ================================

# --- FAKE HOME PAGE FOR STEALTH MODE ---
fake_home_template = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Md. Rahim - Web Developer</title>
    <style>
        body { font-family: sans-serif; background: #f4f4f4; color: #333; text-align: center; padding: 50px; }
        h1 { color: #555; }
        .card { background: white; padding: 20px; max-width: 600px; margin: 0 auto; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .btn { display: inline-block; padding: 10px 20px; background: #007BFF; color: white; text-decoration: none; border-radius: 5px; margin-top: 10px; }
    </style>
</head>
<body>
    <div class="card">
        <h1>👨‍💻 Md. Rahim</h1>
        <p>Full Stack Web Developer | Python | Flask | MongoDB</p>
        <p>I build scalable web applications and automation bots.</p>
        <a href="mailto:contact@example.com" class="btn">Contact Me</a>
    </div>
</body>
</html>
"""

index_template = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <!-- ANTIBAN: GOOGLE INDEXING OFF -->
    <meta name="robots" content="noindex, nofollow">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>{{ site_name }} - Home</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swiper@11/swiper-bundle.min.css" />

    <style>
        @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700&display=swap');
        @import url('https://fonts.googleapis.com/css2?family=Hind+Siliguri:wght@400;500;600;700&display=swap');
        
        :root { --primary: #E50914; --dark: #0f1012; --card-bg: #1a1a1a; --text: #fff; --red-btn: #cc0000; --blue-badge: #0084ff; }
        * { margin: 0; padding: 0; box-sizing: border-box; font-family: 'Poppins', sans-serif; -webkit-tap-highlight-color: transparent; }
        body { background-color: var(--dark); color: var(--text); padding-bottom: 70px; }
        a { text-decoration: none; color: inherit; }
        
        .navbar { display: flex; justify-content: space-between; align-items: center; padding: 12px 15px; background: #161616; border-bottom: 1px solid #222; position: sticky; top: 0; z-index: 100; }
        .logo { font-size: 22px; font-weight: 800; color: var(--primary); text-transform: uppercase; letter-spacing: 1px; }
        
        /* 18+ Toggle */
        .adult-control { display: flex; align-items: center; gap: 8px; font-size: 11px; font-weight: 600; background: #222; padding: 5px 10px; border-radius: 20px; border: 1px solid #333; }
        .switch { position: relative; display: inline-block; width: 34px; height: 18px; }
        .switch input { opacity: 0; width: 0; height: 0; }
        .slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: #4caf50; transition: .4s; border-radius: 34px; }
        .slider:before { position: absolute; content: ""; height: 14px; width: 14px; left: 2px; bottom: 2px; background-color: white; transition: .4s; border-radius: 50%; }
        input:checked + .slider { background-color: #E50914; }
        input:checked + .slider:before { transform: translateX(16px); }

        /* Blur Logic */
        body.hide-adult .is-adult img { filter: blur(20px); pointer-events: none; }
        body.hide-adult .is-adult .card-title { opacity: 0.3; filter: blur(3px); }
        body.hide-adult .is-adult::after { 
            content: "18+"; position: absolute; top: 50%; left: 50%; 
            transform: translate(-50%, -50%); background: #E50914; color: #fff; 
            padding: 5px 10px; font-weight: bold; font-size: 14px; border-radius: 5px; 
            pointer-events: none; z-index: 5;
        }

        .category-container { padding: 10px; background: #121212; display: flex; flex-wrap: wrap; justify-content: center; gap: 8px; }
        .cat-btn { background: var(--red-btn); color: white; padding: 6px 10px; border-radius: 6px; font-size: 12px; font-weight: 700; text-transform: uppercase; display: inline-flex; align-items: center; gap: 5px; border: 1px solid #990000; box-shadow: 0 3px 0 #800000; transition: 0.1s; white-space: nowrap; }
        .cat-btn:active { transform: translateY(3px); box-shadow: none; }
        .cat-btn.active { background: #ffcc00; color: #000; border-color: #cc9900; box-shadow: 0 3px 0 #997700; }
        .request-btn { background: #28a745; border-color: #28a745; }

        .search-wrapper { padding: 5px 15px 15px 15px; background: #121212; display: flex; justify-content: center; }
        .big-search-box { width: 100%; max-width: 600px; display: flex; background: #1e252b; border: 2px solid #00c3ff; border-radius: 8px; overflow: hidden; }
        .big-search-box input { flex: 1; background: transparent; border: none; padding: 10px 15px; color: #fff; font-family: 'Hind Siliguri', sans-serif; font-size: 15px; outline: none; }
        .big-search-box button { background: #00c3ff; border: none; width: 50px; cursor: pointer; color: #fff; font-size: 18px; }

        /* HERO SLIDER */
        .slider-section { padding: 10px 15px; margin-bottom: 10px; }
        .swiper { width: 100%; height: 200px; border-radius: 8px; overflow: hidden; }
        @media (min-width: 600px) { .swiper { height: 320px; } }
        .swiper-slide { position: relative; background: #000; display: flex; align-items: flex-end; }
        .slide-img { width: 100%; height: 100%; object-fit: cover; opacity: 0.8; }
        .slide-overlay { position: absolute; inset: 0; background: linear-gradient(to top, rgba(0,0,0,1) 0%, rgba(0,0,0,0.6) 40%, transparent 100%); pointer-events: none; }
        .slide-content { position: absolute; bottom: 0; left: 0; width: 100%; padding: 15px; z-index: 10; padding-right: 90px; }
        .slide-title { font-size: 1.4rem; font-weight: 700; line-height: 1.2; text-shadow: 0 2px 4px rgba(0,0,0,0.8); margin-bottom: 5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; text-transform: uppercase; }
        .slide-meta { font-size: 0.9rem; color: #ccc; font-weight: 500; }
        .type-badge { position: absolute; bottom: 0; right: 0; background: var(--blue-badge); color: #fff; padding: 6px 15px; font-weight: 700; font-size: 0.85rem; text-transform: uppercase; border-top-left-radius: 8px; z-index: 20; }
        .swiper-pagination-bullet { background: #888; opacity: 1; width: 8px; height: 8px; }
        .swiper-pagination-bullet-active { background: #fff; width: 20px; border-radius: 4px; }

        .section { padding: 0 15px; }
        .section-header { margin-bottom: 15px; border-left: 4px solid var(--primary); padding-left: 10px; }
        .section-title { font-size: 1.1rem; font-weight: 700; text-transform: uppercase; }

        .grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
        @media (min-width: 600px) { .grid { grid-template-columns: repeat(3, 1fr); gap: 15px; } }
        @media (min-width: 900px) { .grid { grid-template-columns: repeat(5, 1fr); gap: 20px; } }

        .card { position: relative; background: var(--card-bg); border-radius: 6px; overflow: hidden; aspect-ratio: 2/3; transition: transform 0.2s; }
        .card-img { width: 100%; height: 100%; object-fit: cover; }
        .card-overlay { position: absolute; inset: 0; background: linear-gradient(to top, rgba(0,0,0,0.95) 0%, transparent 60%); display: flex; flex-direction: column; justify-content: flex-end; padding: 10px; }
        .card-title { font-size: 0.85rem; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .card-meta { font-size: 0.75rem; color: #ccc; display: flex; justify-content: space-between; }
        .rating-badge { position: absolute; top: 6px; left: 6px; background: rgba(0,0,0,0.7); color: #ffb400; padding: 2px 5px; border-radius: 3px; font-size: 0.65rem; font-weight: bold; }

        .pagination { display: flex; justify-content: center; gap: 10px; margin: 30px 0; }
        .page-btn { padding: 8px 16px; background: #222; border-radius: 4px; color: #fff; font-size: 0.9rem; border: 1px solid #333; }

        .bottom-nav { position: fixed; bottom: 0; width: 100%; background: #161616; display: flex; justify-content: space-around; padding: 10px 0; border-top: 1px solid #252525; z-index: 99; }
        .nav-item { display: flex; flex-direction: column; align-items: center; color: #777; font-size: 10px; }
        .nav-item.active { color: var(--primary); }
        .ad-container { margin: 15px 0; text-align: center; overflow: hidden; }
    </style>
</head>
<body class="hide-adult"> 

<nav class="navbar">
    <a href="/" class="logo">{{ site_name }}</a>
    <div class="adult-control">
        <span id="adult-label" style="color:#4caf50;">18+ OFF</span>
        <label class="switch">
            <input type="checkbox" id="adultToggle">
            <span class="slider"></span>
        </label>
    </div>
</nav>

<div class="category-container">
    <a href="/" class="cat-btn {{ 'active' if not selected_cat and not request.args.get('type') else '' }}">🏠 Home</a>
    <a href="/?type=movie" class="cat-btn {{ 'active' if request.args.get('type') == 'movie' else '' }}"><i class="fas fa-film"></i> All Movies</a>
    <a href="/?type=series" class="cat-btn {{ 'active' if request.args.get('type') == 'series' else '' }}"><i class="fas fa-tv"></i> All Web Series</a>
    {% for cat in categories %}
    <a href="/?cat={{ cat.name }}" class="cat-btn {{ 'active' if selected_cat == cat.name else '' }}">
        {% if 'Bangla' in cat.name %}🇧🇩{% elif 'Hindi' in cat.name %}🇮🇳{% elif 'English' in cat.name %}🇺🇸{% else %}<i class="fas fa-tag"></i>{% endif %} {{ cat.name }}
    </a>
    {% endfor %}
    <a href="https://t.me/{{ BOT_USERNAME }}" class="cat-btn request-btn"><i class="fas fa-comment-dots"></i> Request Movie</a>
</div>

<div class="search-wrapper">
    <form action="/" method="GET" class="big-search-box">
        <input type="text" name="q" placeholder="সার্চ করে খুঁজে নিন পছন্দের মুভি..." value="{{ query }}">
        <button type="submit"><i class="fas fa-search"></i></button>
    </form>
</div>

{% if slider_movies %}
<div class="slider-section">
    <div class="swiper mySwiper">
        <div class="swiper-wrapper">
            {% for slide in slider_movies %}
            <div class="swiper-slide {% if slide.is_adult %}is-adult{% endif %}">
                <a href="{{ url_for('movie_detail', movie_id=slide._id) }}" style="width:100%; height:100%; position:relative;">
                    <img src="{{ slide.backdrop or slide.poster }}" class="slide-img">
                    <div class="slide-overlay"></div>
                    <div class="slide-content">
                        <h2 class="slide-title">{{ slide.title }}</h2>
                        <div class="slide-meta">{{ (slide.release_date or '')[:4] }} • {{ slide.language }}</div>
                    </div>
                    <div class="type-badge">{{ slide.type|upper if slide.type else 'MOVIE' }}</div>
                </a>
            </div>
            {% endfor %}
        </div>
        <div class="swiper-pagination"></div>
    </div>
</div>
{% endif %}

<main class="section">
    {% if ad_settings.banner_ad %}<div class="ad-container">{{ ad_settings.banner_ad|safe }}</div>{% endif %}

    <div class="section-header">
        <h2 class="section-title">
            {% if request.args.get('type') == 'movie' %} All Movies
            {% elif request.args.get('type') == 'series' %} All Web Series
            {% elif selected_cat %} {{ selected_cat }}
            {% elif query %} Search Results
            {% else %} Latest Uploads {% endif %}
        </h2>
    </div>

    <div class="grid">
        {% for movie in movies %}
        <a href="{{ url_for('movie_detail', movie_id=movie._id) }}" class="card {% if movie.is_adult %}is-adult{% endif %}">
            <span class="rating-badge">{{ movie.vote_average }}</span>
            <img src="{{ movie.poster or 'https://via.placeholder.com/300x450' }}" class="card-img" loading="lazy">
            <div class="card-overlay">
                <h3 class="card-title">{{ movie.title }}</h3>
                <div class="card-meta">
                    <span>{{ (movie.release_date or '')[:4] }}</span>
                    <span>{{ movie.category or 'Movie' }}</span>
                </div>
            </div>
        </a>
        {% endfor %}
    </div>

    <div class="pagination">
        {% if page > 1 %}
        <a href="/?page={{ page-1 }}&type={{ request.args.get('type') or '' }}&cat={{ selected_cat or '' }}&q={{ query or '' }}" class="page-btn">Previous</a>
        {% endif %}
        {% if has_next %}
        <a href="/?page={{ page+1 }}&type={{ request.args.get('type') or '' }}&cat={{ selected_cat or '' }}&q={{ query or '' }}" class="page-btn">Next</a>
        {% endif %}
    </div>
    <div style="height: 20px;"></div>
</main>

<nav class="bottom-nav">
    <a href="/" class="nav-item {{ 'active' if not request.args.get('type') else '' }}"><i class="fas fa-home"></i>Home</a>
    <a href="/?type=movie" class="nav-item {{ 'active' if request.args.get('type') == 'movie' else '' }}"><i class="fas fa-film"></i>Movies</a>
    <a href="/?type=series" class="nav-item {{ 'active' if request.args.get('type') == 'series' else '' }}"><i class="fas fa-tv"></i>Series</a>
</nav>

{% if ad_settings.popunder %}{{ ad_settings.popunder|safe }}{% endif %}

<script src="https://cdn.jsdelivr.net/npm/swiper@11/swiper-bundle.min.js"></script>
<script>
    var swiper = new Swiper(".mySwiper", {
        spaceBetween: 15,
        centeredSlides: true,
        autoplay: { delay: 3500, disableOnInteraction: false },
        pagination: { el: ".swiper-pagination", clickable: true, dynamicBullets: true },
        loop: true
    });

    const toggle = document.getElementById('adultToggle');
    const label = document.getElementById('adult-label');
    const body = document.body;

    if (localStorage.getItem('adult_enabled') === 'true') {
        body.classList.remove('hide-adult');
        toggle.checked = true;
        label.innerText = "18+ ON";
        label.style.color = "#E50914";
    } else {
        body.classList.add('hide-adult');
        toggle.checked = false;
        label.innerText = "18+ OFF";
        label.style.color = "#4caf50";
    }

    toggle.addEventListener('change', function() {
        if(this.checked) {
            if(confirm("Are you over 18 years old? This will show adult content.")) {
                body.classList.remove('hide-adult');
                localStorage.setItem('adult_enabled', 'true');
                label.innerText = "18+ ON";
                label.style.color = "#E50914";
            } else {
                this.checked = false;
            }
        } else {
            body.classList.add('hide-adult');
            localStorage.setItem('adult_enabled', 'false');
            label.innerText = "18+ OFF";
            label.style.color = "#4caf50";
        }
    });
</script>

</body>
</html>
"""

# --- DETAIL TEMPLATE (Updated with REQUEST BUTTON) ---
detail_template = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="robots" content="noindex, nofollow"> <!-- ANTIBAN -->
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>{{ movie.title }} - Download</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600;700&display=swap');
        :root { --primary: #E50914; --dark: #0f0f0f; --bg-sec: #1a1a1a; --text: #eee; }
        body { background-color: var(--dark); color: var(--text); font-family: 'Poppins', sans-serif; padding-bottom: 30px; }
        .container { max-width: 900px; margin: 0 auto; padding: 15px; }
        
        .backdrop { height: 250px; position: relative; overflow: hidden; margin-bottom: -80px; }
        .backdrop img { width: 100%; height: 100%; object-fit: cover; opacity: 0.6; mask-image: linear-gradient(to bottom, black 50%, transparent 100%); }
        .back-btn { position: absolute; top: 15px; left: 15px; background: rgba(0,0,0,0.6); color: #fff; width: 35px; height: 35px; display: flex; align-items: center; justify-content: center; border-radius: 50%; z-index: 10; font-size: 14px; }
        
        .movie-info { position: relative; display: flex; flex-direction: column; align-items: center; text-align: center; gap: 15px; z-index: 5; }
        .poster-box { width: 140px; border-radius: 8px; box-shadow: 0 5px 15px rgba(0,0,0,0.5); overflow: hidden; border: 2px solid #333; }
        .poster-box img { width: 100%; display: block; }
        
        h1 { font-size: 1.6rem; margin-bottom: 5px; line-height: 1.2; }
        .meta-tags { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; margin-bottom: 8px; font-size: 0.8rem; color: #bbb; }
        .tag { background: #333; padding: 3px 8px; border-radius: 4px; }
        .overview { font-size: 0.9rem; line-height: 1.6; color: #ccc; margin-bottom: 25px; text-align: justify; }
        
        .file-section { background: var(--bg-sec); border-radius: 8px; padding: 15px; border: 1px solid #2a2a2a; }
        .section-head { font-size: 1rem; margin-bottom: 15px; display: flex; align-items: center; gap: 10px; color: var(--primary); font-weight: 600; border-bottom: 1px solid #333; padding-bottom: 10px; }
        
        .file-item { display: flex; flex-direction: column; align-items: center; background: #252525; padding: 15px; border-radius: 8px; margin-bottom: 12px; text-align: center; }
        .file-details h4 { font-size: 1rem; margin-bottom: 4px; color: #fff; }
        .file-details span { font-size: 0.8rem; color: #999; }
        
        .btn-dl { 
            background: #0088cc; 
            color: white; 
            width: 100%;
            padding: 10px; 
            margin-top: 10px; 
            border-radius: 6px; 
            text-decoration: none; 
            font-weight: 600; 
            display: flex; 
            align-items: center; 
            justify-content: center; 
            gap: 8px;
            font-size: 0.95rem;
            transition: 0.3s;
            cursor: pointer;
            border: none;
        }
        .btn-dl:hover { background: #0077b5; transform: translateY(-2px); }

        .badge-q { padding: 3px 8px; border-radius: 4px; font-size: 0.7rem; font-weight: bold; }
        .q-4k { background: #d63384; color: #fff; box-shadow: 0 0 10px rgba(214, 51, 132, 0.5); }
        .q-1080p { background: #6f42c1; color: #fff; }
        .q-720p { background: #0d6efd; color: #fff; }
        .q-480p { background: #198754; color: #fff; }
        
        .dmca-link { color: #555; font-size: 11px; text-decoration: none; margin-top: 30px; display: block; text-align: center; }
        .dmca-link:hover { text-decoration: underline; color: #777; }
        
        .report-btn { color: #dc3545; background: none; border: 1px solid #dc3545; padding: 5px 10px; border-radius: 4px; font-size: 0.75rem; margin-top: 10px; cursor: pointer; text-decoration: none; display: inline-block; }
        .report-btn:hover { background: #dc3545; color: white; }

        @media (min-width: 600px) {
            .movie-info { flex-direction: row; text-align: left; align-items: flex-end; padding: 0 20px; }
            .meta-tags { justify-content: flex-start; }
            .overview { text-align: left; padding: 0 20px; }
            .backdrop { height: 350px; margin-bottom: -100px; }
            .poster-box { width: 180px; }
        }
    </style>
</head>
<body>

<a href="/" class="back-btn"><i class="fas fa-arrow-left"></i></a>

<div class="backdrop">
    <img src="{{ movie.backdrop or movie.poster }}" alt="">
</div>

<div class="container">
    <div class="movie-info">
        <div class="poster-box">
            <img src="{{ movie.poster }}" alt="Poster">
        </div>
        <div style="padding-bottom: 10px;">
            <h1>{{ movie.title }}</h1>
            <div class="meta-tags">
                <span class="tag"><i class="fas fa-star" style="color:#ffb400"></i> {{ movie.vote_average }}</span>
                <span class="tag">{{ (movie.release_date or 'N/A')[:4] }}</span>
                {% if movie.runtime %}<span class="tag"><i class="far fa-clock"></i> {{ movie.runtime }} min</span>{% endif %}
                <span class="tag" style="background: var(--primary); color: #fff;">{{ movie.language or 'Eng' }}</span>
                <span class="tag">{{ movie.category or 'Movie' }}</span>
            </div>
            <div style="margin-top:5px; font-size: 0.85rem; color: #aaa;">
                {% if movie.genres %}
                    {{ movie.genres|join(', ') }}
                {% endif %}
            </div>
        </div>
    </div>
    
    <div style="height: 20px;"></div>
    
    {% if movie.is_adult %}
    <div style="background: #330000; color: #ff9999; padding: 10px; border-radius: 5px; border: 1px solid #990000; font-size: 0.85rem; margin-bottom: 15px; text-align: center;">
        <i class="fas fa-exclamation-triangle"></i> <b>WARNING:</b> This content contains 18+ Adult material.
    </div>
    {% endif %}
    
    <p class="overview">{{ movie.overview }}</p>

    {% if movie.trailer %}
    <div style="margin-bottom: 25px; padding: 0 5px;">
        <div class="section-head"><i class="fab fa-youtube"></i> Watch Trailer</div>
        <div style="position: relative; padding-bottom: 56.25%; height: 0; overflow: hidden; border-radius: 8px; border: 1px solid #333;">
            <iframe style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; border:0;" 
                    src="https://www.youtube.com/embed/{{ movie.trailer }}" allowfullscreen></iframe>
        </div>
    </div>
    {% endif %}

    {% if movie.cast %}
    <div style="margin-bottom: 25px; padding: 0 5px;">
        <div class="section-head"><i class="fas fa-users"></i> Top Cast</div>
        <div style="display: flex; gap: 15px; overflow-x: auto; padding-bottom: 10px; scrollbar-width: none;">
            {% for actor in movie.cast %}
            <div style="min-width: 90px; text-align: center;">
                <img src="{{ actor.img or 'https://via.placeholder.com/90x90?text=No+Img' }}" 
                     style="width: 80px; height: 80px; border-radius: 50%; object-fit: cover; border: 2px solid #333; margin-bottom: 5px;">
                <div style="font-size: 0.75rem; color: #ccc; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">{{ actor.name }}</div>
            </div>
            {% endfor %}
        </div>
    </div>
    {% endif %}

    {% if ad_settings.banner_ad %}<div style="margin: 20px 0; text-align:center;">{{ ad_settings.banner_ad|safe }}</div>{% endif %}

    <div style="display: flex; gap: 10px; margin-bottom: 20px;">
        <a href="whatsapp://send?text=Download {{ movie.title }} - {{ request.url }}" class="btn-dl" style="background: #25D366; flex: 1; margin:0;">
            <i class="fab fa-whatsapp"></i> Share
        </a>
        <a href="https://www.facebook.com/sharer/sharer.php?u={{ request.url }}" target="_blank" class="btn-dl" style="background: #1877F2; flex: 1; margin:0;">
            <i class="fab fa-facebook-f"></i> Share
        </a>
        <button onclick="navigator.clipboard.writeText(window.location.href); alert('Link Copied!')" class="btn-dl" style="background: #333; flex: 1; margin:0;">
            <i class="fas fa-link"></i> Copy
        </button>
    </div>

    <div class="file-section">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:15px; border-bottom:1px solid #333; padding-bottom:10px;">
             <span class="section-head" style="margin:0;"><i class="fas fa-download"></i> Download Links</span>
             {% if movie.files %}
                <a href="/report/broken/{{ movie._id }}" class="report-btn" onclick="return confirm('Report broken link for this movie?')"><i class="fas fa-bug"></i> Report Broken Link</a>
             {% endif %}
        </div>

        {% if movie.files %}
            {% for file in movie.files|reverse %}
            <div class="file-item">
                <div class="file-details">
                    {% if file.episode_label %}
                        <h4 style="color: #ffb400; font-weight: 700;">{{ file.episode_label }}</h4>
                        {% set q_class = 'q-480p' %}
                        {% if '1080p' in file.quality %} {% set q_class = 'q-1080p' %}
                        {% elif '720p' in file.quality %} {% set q_class = 'q-720p' %}
                        {% elif '4K' in file.quality %} {% set q_class = 'q-4k' %}
                        {% endif %}
                        <span class="badge-q {{ q_class }}">{{ file.quality }}</span>
                    {% else %}
                        <h4>{{ file.quality }}</h4>
                    {% endif %}
                    
                    <div style="font-size: 0.75rem; color: #888; margin-top: 3px;">
                        Size: {{ file.size }} • Format: {{ file.file_type|upper }}
                    </div>
                    <div style="font-size: 0.65rem; color: #555; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 250px;">
                        {{ file.filename }}
                    </div>
                </div>
                
                {% set tg_link = "https://t.me/" + BOT_USERNAME + "?start=" + file.unique_code %}
                
                {% if ad_settings.shortener_domain and ad_settings.shortener_api %}
                    <!-- JS Button for API Shorteners -->
                    <button class="btn-dl" onclick="processLink(this, '{{ tg_link }}', '{{ ad_settings.shortener_api }}', '{{ ad_settings.shortener_domain }}')">
                        <i class="fab fa-telegram-plane"></i> 
                        {% if file.episode_label %}Watch {{ file.episode_label }}{% else %}Get File{% endif %}
                    </button>
                {% else %}
                    <!-- Direct Link -->
                    <a href="{{ tg_link }}" class="btn-dl" target="_blank">
                        <i class="fab fa-telegram-plane"></i> 
                        {% if file.episode_label %}Watch {{ file.episode_label }}{% else %}Get File{% endif %}
                    </a>
                {% endif %}

            </div>
            {% endfor %}
        {% else %}
            <!-- NO FILES - SHOW REQUEST BUTTON -->
            <div style="text-align: center; padding: 30px 10px;">
                <i class="fas fa-folder-open" style="font-size: 40px; color: #444; margin-bottom: 15px;"></i>
                <h3 style="font-size: 1.2rem; color: #ccc;">No Download Links Available Yet</h3>
                <p style="font-size: 0.9rem; color: #777; margin-bottom: 20px;">This movie has been listed but files are not uploaded yet.</p>
                
                <a href="{{ ADMIN_CONTACT_URL or '#' }}" target="_blank" class="btn-dl" style="background: #e50914; animation: pulse 2s infinite; width: auto; display: inline-flex; padding: 10px 30px;">
                    <i class="fas fa-paper-plane"></i> Request Admin to Upload
                </a>
            </div>
            
            <style>
                @keyframes pulse {
                    0% { box-shadow: 0 0 0 0 rgba(229, 9, 20, 0.7); }
                    70% { box-shadow: 0 0 0 10px rgba(229, 9, 20, 0); }
                    100% { box-shadow: 0 0 0 0 rgba(229, 9, 20, 0); }
                }
            </style>
        {% endif %}
    </div>

    <!-- GLOBAL DOWNLOAD TUTORIAL -->
    {% if ad_settings.tutorial_video %}
    <div style="margin-top: 25px; padding: 0 5px;">
        <div class="section-head"><i class="fas fa-graduation-cap"></i> How to Download</div>
        <div style="position: relative; padding-bottom: 56.25%; height: 0; overflow: hidden; border-radius: 8px; border: 1px solid #333;">
            <iframe style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; border:0;" 
                    src="https://www.youtube.com/embed/{{ ad_settings.tutorial_video }}" allowfullscreen></iframe>
        </div>
        <p style="text-align:center; font-size:0.8rem; color:#aaa; margin-top:5px;">Watch this video if you face any issues downloading.</p>
    </div>
    {% endif %}
    
    <!-- AUTO DMCA REMOVAL BUTTON -->
    <a href="/dmca/report/{{ movie._id }}" class="dmca-link" onclick="return confirm('⚠️ Are you the Copyright Owner?\\n\\nThis will DELETE the content from our database immediately.\\n\\nProceed?')">
        Report Copyright / Remove Content (DMCA)
    </a>

    <div style="text-align: center; margin-top: 20px; font-size: 0.8rem; color: #555;">
        &copy; {{ site_name }} 2025
    </div>
</div>

{% if ad_settings.popunder %}{{ ad_settings.popunder|safe }}{% endif %}

<script>
    async function processLink(btn, originalUrl, apiKey, domain) {
        // বাটন লোডিং স্টেট
        const originalText = btn.innerHTML;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Please Wait...';
        btn.style.opacity = "0.7";
        btn.disabled = true;

        try {
            // FIX: Call our own server-side proxy instead of external API directly to avoid CORS
            const proxyUrl = `/api/shorten?api=${apiKey}&domain=${domain}&url=${encodeURIComponent(originalUrl)}`;
            
            const response = await fetch(proxyUrl);
            const data = await response.json();

            // Check for various response formats (shortenedUrl, link, short, etc.)
            let finalLink = null;
            
            if (data.status === 'success' || data.shortenedUrl) {
                finalLink = data.shortenedUrl || data.link || data.short;
            } else if (data.shortenedUrl) {
                finalLink = data.shortenedUrl;
            }

            if (finalLink) {
                // সফল হলে শর্ট লিংকে রিডাইরেক্ট
                window.location.href = finalLink;
            } else {
                // ফেইল হলে অরিজিনাল লিংকে
                console.error("API returned error or unknown format:", data);
                // alert("Shortener Error! Redirecting to original link...");
                window.location.href = originalUrl;
            }
        } catch (error) {
            console.error("Fetch Error:", error);
            // কোন এরর হলে অরিজিনাল লিংকে রিডাইরেক্ট
            window.location.href = originalUrl;
        } finally {
            // যদি পেজ রিডাইরেক্ট না হয়, বাটন ঠিক করা
            setTimeout(() => {
                btn.innerHTML = originalText;
                btn.style.opacity = "1";
                btn.disabled = false;
            }, 3000);
        }
    }
</script>

</body>
</html>
"""

# ================================
#        ADMIN PANEL TEMPLATES
# ================================

admin_base = """
<!DOCTYPE html>
<html lang="en" data-bs-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Panel - MovieZone</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #0f1012; }
        .sidebar { height: 100vh; position: fixed; top: 0; left: 0; width: 240px; background: #191b1f; padding-top: 20px; border-right: 1px solid #2a2d31; }
        .sidebar a { padding: 12px 25px; display: block; color: #aaa; text-decoration: none; transition: 0.3s; font-weight: 500; }
        .sidebar a:hover, .sidebar a.active { color: #fff; background: #E50914; border-radius: 0 25px 25px 0; }
        .sidebar .brand { font-size: 22px; font-weight: bold; color: #E50914; text-align: center; margin-bottom: 30px; }
        .main-content { margin-left: 240px; padding: 30px; }
        .card { background: #1f2226; border: 1px solid #2a2d31; }
        .form-control { background: #131517; border-color: #333; color: #fff; }
        .form-control:focus { background: #131517; color: #fff; border-color: #E50914; box-shadow: none; }
        .poster-preview { width: 100%; border-radius: 8px; max-width: 200px; }
        @media (max-width: 768px) {
            .sidebar { width: 60px; }
            .sidebar a span, .sidebar .brand span { display: none; }
            .sidebar a { padding: 15px; text-align: center; border-radius: 0; }
            .main-content { margin-left: 60px; padding: 15px; }
        }
    </style>
</head>
<body>

<div class="sidebar">
    <div class="brand"><i class="fas fa-play-circle"></i> <span>Admin</span></div>
    <a href="/admin" class="{{ 'active' if active == 'dashboard' else '' }}"><i class="fas fa-th-large"></i> <span>Movies</span></a>
    <a href="/admin/categories" class="{{ 'active' if active == 'categories' else '' }}"><i class="fas fa-tags"></i> <span>Categories</span></a>
    <a href="/admin/settings" class="{{ 'active' if active == 'settings' else '' }}"><i class="fas fa-cogs"></i> <span>Settings</span></a>
    <a href="/" target="_blank"><i class="fas fa-external-link-alt"></i> <span>View Site</span></a>
</div>

<div class="main-content">
    <!-- CONTENT_GOES_HERE -->
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

admin_dashboard = """
<div class="d-flex justify-content-between align-items-center mb-4">
    <h2>Manage Movies</h2>
    <form class="d-flex" method="GET">
        <input class="form-control me-2" type="search" name="q" placeholder="Search movies..." value="{{ q }}">
        <button class="btn btn-outline-light" type="submit">Search</button>
    </form>
</div>

<div class="row">
    {% for movie in movies %}
    <div class="col-md-6 col-lg-4 col-xl-3 mb-4">
        <div class="card h-100">
            <div class="row g-0 h-100">
                <div class="col-4">
                    <img src="{{ movie.poster or 'https://via.placeholder.com/150' }}" class="img-fluid rounded-start h-100" style="object-fit:cover;" alt="...">
                </div>
                <div class="col-8">
                    <div class="card-body p-2 d-flex flex-column h-100">
                        <h6 class="card-title mb-1 text-truncate">{{ movie.title }}</h6>
                        <span class="badge bg-danger mb-1">{{ movie.category }}</span>
                        {% if movie.is_adult %}<span class="badge bg-warning text-dark">18+</span>{% endif %}
                        <p class="card-text small text-muted mb-1">{{ (movie.release_date or '')[:4] }}</p>
                        <div class="mt-auto d-flex gap-2">
                            <a href="/admin/movie/edit/{{ movie._id }}" class="btn btn-sm btn-primary flex-grow-1">Edit</a>
                            <a href="/admin/movie/delete/{{ movie._id }}" onclick="return confirm('Are you sure?')" class="btn btn-sm btn-danger"><i class="fas fa-trash"></i></a>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    {% endfor %}
</div>

<div class="d-flex justify-content-center mt-4">
    {% if page > 1 %}
    <a href="?page={{ page-1 }}&q={{ q }}" class="btn btn-outline-secondary me-2">Previous</a>
    {% endif %}
    <span class="align-self-center mx-2">Page {{ page }}</span>
    <a href="?page={{ page+1 }}&q={{ q }}" class="btn btn-outline-secondary ms-2">Next</a>
</div>
"""

admin_categories = """
<div class="container" style="max-width: 600px;">
    <h3 class="mb-4">Manage Categories</h3>
    
    <div class="card p-3 mb-4">
        <form method="POST" class="d-flex gap-2">
            <input type="text" name="new_category" class="form-control" placeholder="New Category Name (e.g. Bollywood)" required>
            <button class="btn btn-success" type="submit">Add</button>
        </form>
    </div>

    <div class="list-group">
        {% for cat in categories %}
        <div class="list-group-item d-flex justify-content-between align-items-center bg-dark text-white border-secondary">
            <span>{{ cat.name }}</span>
            <a href="/admin/categories/delete/{{ cat._id }}" class="btn btn-sm btn-danger"><i class="fas fa-trash"></i></a>
        </div>
        {% endfor %}
    </div>
</div>
"""

admin_edit = """
<div class="container-fluid">
    <div class="d-flex justify-content-between align-items-center mb-4">
        <h3>Edit Content: <span class="text-primary">{{ movie.title }}</span></h3>
        <a href="/admin" class="btn btn-secondary btn-sm">Back</a>
    </div>

    <div class="row">
        <!-- TMDB Search & ID Column -->
        <div class="col-md-4 mb-4">
            
            <!-- Combined Smart Input Box -->
            <div class="card p-3 mb-3 border-primary shadow-sm">
                <h6 class="text-primary"><i class="fas fa-magic"></i> Auto Import</h6>
                <div class="form-text mb-2">Paste ANY Link (IMDb/TMDB) or Name here:</div>
                <div class="input-group">
                    <input type="text" id="smartInput" class="form-control" placeholder="Paste Link or Type Name..." value="{{ movie.title }}">
                    <button class="btn btn-primary" type="button" onclick="smartFetch()"><i class="fas fa-search"></i> Fetch</button>
                </div>
            </div>

            <!-- Results Area -->
            <div class="card p-3">
                <h6 class="border-bottom pb-2">Found Matches:</h6>
                <div id="tmdbResults" style="max-height: 400px; overflow-y: auto; min-height: 50px;">
                    <div class="text-muted small text-center mt-3">Results will appear here...</div>
                </div>
            </div>
            
            <div class="card p-3 mt-3 text-center">
                <label class="form-label text-muted">Current Poster</label><br>
                <img src="{{ movie.poster }}" class="poster-preview img-thumbnail" style="max-height: 200px;">
            </div>
        </div>

        <!-- Edit Form Column -->
        <div class="col-md-8">
            <div class="card p-4 shadow-sm">
                <form method="POST">
                    <div class="row">
                        <div class="col-md-12 mb-3">
                            <label class="form-label fw-bold">Title</label>
                            <input type="text" name="title" class="form-control form-control-lg" value="{{ movie.title }}" required>
                        </div>
                    </div>

                    <div class="row">
                        <div class="col-md-6 mb-3">
                            <label class="form-label">Category</label>
                            <select name="category" class="form-select">
                                <option value="Uncategorized">Select Category...</option>
                                {% for cat in categories %}
                                <option value="{{ cat.name }}" {{ 'selected' if movie.category == cat.name else '' }}>{{ cat.name }}</option>
                                {% endfor %}
                            </select>
                        </div>

                        <div class="col-md-6 mb-3">
                            <label class="form-label">Content Type</label>
                            <select name="type" class="form-select bg-dark text-white">
                                <option value="movie" {{ 'selected' if movie.type == 'movie' else '' }}>Movie</option>
                                <option value="series" {{ 'selected' if movie.type == 'series' else '' }}>Web Series</option>
                            </select>
                        </div>
                    </div>
                    
                    <div class="mb-3">
                         <label class="form-label text-warning fw-bold">18+ Content Status</label>
                         <select name="is_adult" class="form-select border-warning">
                             <option value="false" {{ 'selected' if not movie.is_adult else '' }}>NO (Safe for Everyone)</option>
                             <option value="true" {{ 'selected' if movie.is_adult else '' }}>YES (18+ Adult Content)</option>
                         </select>
                    </div>

                    <div class="mb-3">
                        <label class="form-label">Overview / Story</label>
                        <textarea name="overview" class="form-control" rows="5">{{ movie.overview }}</textarea>
                    </div>

                    <div class="row">
                        <div class="col-md-6 mb-3">
                            <label class="form-label">Poster URL</label>
                            <input type="text" name="poster" class="form-control" value="{{ movie.poster }}">
                        </div>
                        <div class="col-md-6 mb-3">
                            <label class="form-label">Backdrop URL</label>
                            <input type="text" name="backdrop" class="form-control" value="{{ movie.backdrop }}">
                        </div>
                    </div>

                    <div class="row">
                        <div class="col-md-4 mb-3">
                            <label class="form-label">Release Date</label>
                            <input type="text" name="release_date" class="form-control" value="{{ movie.release_date }}">
                        </div>
                        <div class="col-md-4 mb-3">
                            <label class="form-label">Rating</label>
                            <input type="text" name="vote_average" class="form-control" value="{{ movie.vote_average }}">
                        </div>
                        <div class="col-md-4 mb-3">
                            <label class="form-label">Language</label>
                            <input type="text" name="language" class="form-control" value="{{ movie.language }}">
                        </div>
                    </div>

                    <button type="submit" class="btn btn-success btn-lg w-100"><i class="fas fa-save"></i> Update Details</button>
                </form>
            </div>
        </div>
    </div>
</div>

<script>
    function smartFetch() {
        let query = document.getElementById('smartInput').value.trim();
        const resultDiv = document.getElementById('tmdbResults');
        
        if(!query) {
            resultDiv.innerHTML = '<div class="text-warning text-center">Please paste a link or type a name.</div>';
            return;
        }
        
        resultDiv.innerHTML = '<div class="text-center py-3"><div class="spinner-border text-primary" role="status"></div><br>Searching...</div>';
        
        fetch('/admin/api/tmdb?q=' + encodeURIComponent(query))
        .then(r => r.json())
        .then(data => {
            if(data.error) {
                resultDiv.innerHTML = '<div class="alert alert-danger small">'+data.error+'</div>';
                return;
            }
            if(!data.results || data.results.length === 0) {
                 resultDiv.innerHTML = '<div class="alert alert-warning small">No results found. Try using TMDB Link.</div>';
                 return;
            }

            let html = '<div class="list-group list-group-flush">';
            data.results.forEach(item => {
                let title = item.title || item.name;
                let date = item.release_date || item.first_air_date || 'N/A';
                let type = item.media_type || 'movie';
                let poster = item.poster_path ? 'https://image.tmdb.org/t/p/w92' + item.poster_path : 'https://via.placeholder.com/92x138?text=No+Img';
                
                let cleanItem = JSON.stringify(item).replace(/'/g, "&#39;").replace(/"/g, "&quot;");

                html += `<button type="button" class="list-group-item list-group-item-action d-flex align-items-center gap-2 p-2" onclick='fillForm(${cleanItem})'>
                    <img src="${poster}" style="width:45px; height:65px; object-fit:cover; border-radius:4px;">
                    <div style="overflow:hidden; width:100%;">
                        <div class="fw-bold text-truncate">${title}</div>
                        <div class="d-flex justify-content-between small text-muted">
                            <span>${date.substring(0,4)}</span>
                            <span class="badge bg-secondary">${type.toUpperCase()}</span>
                        </div>
                    </div>
                </button>`;
            });
            html += '</div>';
            resultDiv.innerHTML = html;
        });
    }

    function fillForm(data) {
        document.querySelector('input[name="title"]').value = data.title || data.name;
        document.querySelector('textarea[name="overview"]').value = data.overview || '';
        document.querySelector('input[name="release_date"]').value = data.release_date || data.first_air_date || '';
        document.querySelector('input[name="vote_average"]').value = data.vote_average || '';

        if(data.poster_path) {
            let pUrl = 'https://image.tmdb.org/t/p/w500' + data.poster_path;
            document.querySelector('input[name="poster"]').value = pUrl;
            document.querySelector('.poster-preview').src = pUrl;
        }
        if(data.backdrop_path) {
            document.querySelector('input[name="backdrop"]').value = 'https://image.tmdb.org/t/p/w1280' + data.backdrop_path;
        }

        let typeSelect = document.querySelector('select[name="type"]');
        if(data.media_type === 'tv') {
            typeSelect.value = 'series';
        } else {
            typeSelect.value = 'movie';
        }
        
        let adultSelect = document.querySelector('select[name="is_adult"]');
        if(data.adult === true) {
            adultSelect.value = 'true';
        }

        if(data.original_language) {
             let langMap = {'en': 'English', 'hi': 'Hindi', 'bn': 'Bengali', 'ko': 'Korean', 'ja': 'Japanese', 'ta': 'Tamil', 'te': 'Telugu', 'es': 'Spanish', 'fr': 'French'};
             let fullLang = langMap[data.original_language] || data.original_language.toUpperCase();
             document.querySelector('input[name="language"]').value = fullLang;
        }

        const resDiv = document.getElementById('tmdbResults');
        resDiv.innerHTML = '<div class="alert alert-success mt-2 text-center"><i class="fas fa-check-circle"></i> Data Applied!<br>Please check fields and click <b>Update</b>.</div>';
    }
</script>
"""

# --- ADMIN SETTINGS TEMPLATE (Updated with Stealth Mode) ---
admin_settings = """
<div class="container" style="max-width: 800px;">
    <h3 class="mb-4">Website Settings</h3>
    <div class="card p-4">
        <form method="POST">
            
            <!-- STEALTH MODE TOGGLE -->
            <div class="form-check form-switch mb-4 p-3 border rounded border-warning bg-dark">
                <input class="form-check-input" type="checkbox" name="stealth_mode" id="stealth" style="transform: scale(1.5); margin-right: 15px;" {{ 'checked' if settings.stealth_mode else '' }}>
                <label class="form-check-label text-warning fw-bold" for="stealth">
                    <i class="fas fa-user-secret"></i> STEALTH MODE (Anti-Ban Protection)
                </label>
                <div class="form-text text-light mt-2">
                    <b>কিভাবে কাজ করে:</b> এটি অন করলে হোমপেইজে মুভি দেখা যাবে না। একটি সাধারণ পোর্টফোলিও সাইট দেখাবে। 
                    Render/Heroku মডারেটররা ভাববে এটি পার্সোনাল সাইট। মুভি শুধু টেলিগ্রামের ডিরেক্ট লিংক দিয়ে দেখা যাবে।
                    <b>Render ব্যান থেকে বাঁচতে এটি সবসময় ON রাখা ভালো।</b>
                </div>
            </div>

            <div class="row mb-4 border-bottom pb-4">
                <h5 class="text-primary"><i class="fas fa-link"></i> Link Shortener (Monetization)</h5>
                <div class="col-md-6 mb-3">
                    <label class="form-label">Shortener Domain</label>
                    <input type="text" name="shortener_domain" class="form-control" placeholder="e.g. gplinks.com" value="{{ settings.shortener_domain or '' }}">
                    <div class="form-text">Do not put https:// or slash. Just domain.</div>
                </div>
                <div class="col-md-6 mb-3">
                    <label class="form-label">API Key</label>
                    <input type="text" name="shortener_api" class="form-control" placeholder="Paste your API Key here" value="{{ settings.shortener_api or '' }}">
                </div>
            </div>

            <div class="mb-4 border-bottom pb-4">
                <h5 class="text-info"><i class="fab fa-youtube"></i> Download Tutorial</h5>
                <label class="form-label">YouTube Video Link</label>
                <input type="text" name="tutorial_video" class="form-control" placeholder="e.g. https://youtu.be/..." value="{{ settings.tutorial_video_url or '' }}">
                <div class="form-text">This video will appear on every movie page to teach users how to download.</div>
            </div>

            <div class="mb-4">
                <label class="form-label fw-bold">Banner Ad Code (HTML)</label>
                <textarea name="banner_ad" class="form-control" rows="3">{{ settings.banner_ad }}</textarea>
            </div>
            
            <div class="mb-4">
                <label class="form-label fw-bold">Popunder / Scripts (HTML/JS)</label>
                <textarea name="popunder" class="form-control" rows="3">{{ settings.popunder }}</textarea>
            </div>
            
            <button type="submit" class="btn btn-success"><i class="fas fa-save"></i> Save Settings</button>
        </form>
    </div>
</div>
"""

# ================================
#        FLASK ROUTES
# ================================

@app.route('/')
def home():
    # --- STEALTH MODE CHECK ---
    curr_settings = settings.find_one() or {}
    if curr_settings.get('stealth_mode', False):
        # যদি Stealth Mode অন থাকে, তবে চেক করুন ইউজার এডমিন কিনা
        # যদি এডমিন না হয়, তবে ফেইক হোমপেজ দেখান
        if not check_auth():
            return render_template_string(fake_home_template)

    page = int(request.args.get('page', 1))
    per_page = 16
    query = request.args.get('q', '').strip()
    cat_filter = request.args.get('cat', '').strip()
    type_filter = request.args.get('type', '').strip()
    
    db_query = {}
    if query: db_query["title"] = {"$regex": query, "$options": "i"}
    if cat_filter: db_query["category"] = cat_filter
    if type_filter: db_query["type"] = type_filter

    total_movies = movies.count_documents(db_query)
    movie_list = list(movies.find(db_query).sort([('updated_at', -1), ('_id', -1)]).skip((page-1)*per_page).limit(per_page))
    cat_list = list(categories.find())
    
    slider_movies = []
    if not query and not cat_filter and not type_filter:
        slider_movies = list(movies.find({"backdrop": {"$ne": None}}).sort([('created_at', -1)]).limit(5))

    has_next = (page * per_page) < total_movies

    return render_template_string(index_template, movies=movie_list, categories=cat_list, selected_cat=cat_filter, query=query, slider_movies=slider_movies, page=page, has_next=has_next)

@app.route('/movies')
def view_movies():
    return redirect(url_for('home', type='movie'))

@app.route('/series')
def view_series():
    return redirect(url_for('home', type='series'))

@app.route('/movie/<movie_id>')
def movie_detail(movie_id):
    try:
        movie = movies.find_one({"_id": ObjectId(movie_id)})
        if not movie: return "Content Removed or Not Found", 404
        # Inject Admin Contact URL into template context
        return render_template_string(detail_template, movie=movie, ADMIN_CONTACT_URL=ADMIN_CONTACT_URL)
    except:
        return "Invalid ID", 400

# --- NEW: AUTO DMCA DELETE ROUTE ---
@app.route('/dmca/report/<movie_id>')
def dmca_delete(movie_id):
    """ Allows instant removal of content to comply with DMCA without admin intervention """
    try:
        movies.delete_one({"_id": ObjectId(movie_id)})
        return """
        <div style='text-align:center; padding:50px; font-family:sans-serif;'>
            <h1 style='color:green;'>Content Removed Successfully</h1>
            <p>The content has been permanently deleted from our database in compliance with DMCA.</p>
            <p>We respect copyright laws and take immediate action.</p>
            <a href='/' style='background:#333; color:white; padding:10px 20px; text-decoration:none; border-radius:5px;'>Go Home</a>
        </div>
        """
    except:
        return "Error deleting file", 500

# --- NEW: REPORT BROKEN LINK ROUTE ---
@app.route('/report/broken/<movie_id>')
def report_broken(movie_id):
    """ Sends a notification to the admin channel about broken link """
    try:
        movie = movies.find_one({"_id": ObjectId(movie_id)})
        if movie and SOURCE_CHANNEL_ID:
            report_msg = f"⚠️ *BROKEN LINK REPORTED*\n\n🎬 Title: {movie.get('title')}\n🆔 ID: {movie_id}\n\nPlease check the files."
            payload = {'chat_id': SOURCE_CHANNEL_ID, 'text': report_msg, 'parse_mode': 'Markdown'}
            requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload)
        
        return """
        <div style='text-align:center; padding:50px; font-family:sans-serif;'>
            <h1 style='color:#007bff;'>Report Sent!</h1>
            <p>Admin has been notified. We will fix the link soon.</p>
            <a href='javascript:history.back()' style='background:#333; color:white; padding:10px 20px; text-decoration:none; border-radius:5px;'>Go Back</a>
        </div>
        """
    except:
        return "Error sending report", 500

# --- SERVER SIDE SHORTENER PROXY (FIX FOR CORS) ---
@app.route('/api/shorten')
def shorten_link_proxy():
    original_url = request.args.get('url')
    api_key = request.args.get('api')
    domain = request.args.get('domain')

    if not original_url or not api_key or not domain:
        return jsonify({'status': 'error', 'message': 'Missing parameters'})

    # URL Encode the original URL for the API call
    encoded_url = urllib.parse.quote(original_url)
    api_url = f"https://{domain}/api?api={api_key}&url={encoded_url}"

    try:
        # Server-side request (Bypasses Browser CORS)
        resp = requests.get(api_url, timeout=10)
        try:
            data = resp.json()
            return jsonify(data)
        except:
            # If response is not JSON (some shorteners return raw text)
            return jsonify({'status': 'error', 'raw': resp.text})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

# ================================
#        ADMIN ROUTES
# ================================

@app.route('/admin')
def admin_home():
    if not check_auth():
        return Response('Login Required', 401, {'WWW-Authenticate': 'Basic realm="Login Required"'})
    
    page = int(request.args.get('page', 1))
    q = request.args.get('q', '')
    per_page = 20
    
    filter_q = {}
    if q: filter_q['title'] = {'$regex': q, '$options': 'i'}
    
    movie_list = list(movies.find(filter_q).sort('_id', -1).skip((page-1)*per_page).limit(per_page))
    
    full_html = admin_base.replace('<!-- CONTENT_GOES_HERE -->', admin_dashboard)
    return render_template_string(full_html, movies=movie_list, page=page, q=q, active='dashboard')

@app.route('/admin/categories', methods=['GET', 'POST'])
def admin_cats():
    if not check_auth(): return Response('Login Required', 401)
    
    if request.method == 'POST':
        new_cat = request.form.get('new_category').strip()
        if new_cat:
            categories.insert_one({"name": new_cat})
        return redirect(url_for('admin_cats'))
    
    cat_list = list(categories.find())
    full_html = admin_base.replace('<!-- CONTENT_GOES_HERE -->', admin_categories)
    return render_template_string(full_html, categories=cat_list, active='categories')

@app.route('/admin/categories/delete/<cat_id>')
def delete_cat(cat_id):
    if not check_auth(): return Response('Login Required', 401)
    categories.delete_one({"_id": ObjectId(cat_id)})
    return redirect(url_for('admin_cats'))

@app.route('/admin/movie/edit/<movie_id>', methods=['GET', 'POST'])
def admin_edit_movie(movie_id):
    if not check_auth(): return Response('Login Required', 401)
    
    movie = movies.find_one({"_id": ObjectId(movie_id)})
    
    if request.method == 'POST':
        new_poster = request.form.get("poster")
        # Use timezone-aware UTC
        now_utc = datetime.now(datetime.UTC) if hasattr(datetime, 'UTC') else datetime.utcnow()

        update_data = {
            "title": request.form.get("title"),
            "category": request.form.get("category"),
            "language": request.form.get("language"),
            "overview": request.form.get("overview"),
            "poster": new_poster,
            "backdrop": request.form.get("backdrop"),
            "release_date": request.form.get("release_date"),
            "vote_average": request.form.get("vote_average"),
            "type": request.form.get("type"),
            "is_adult": request.form.get("is_adult") == 'true',
            "updated_at": now_utc
        }
        
        movies.update_one({"_id": ObjectId(movie_id)}, {"$set": update_data})
        
        if not movie.get('last_notified') and new_poster and PUBLIC_CHANNEL_ID:
            latest_file = movie.get('files', [])[-1] if movie.get('files') else None
            if latest_file:
                caption = f"🎬 *{escape_markdown(update_data['title'])}*\n"
                if latest_file.get('episode_label'): 
                    caption += f"📌 {escape_markdown(latest_file['episode_label'])}\n"
                
                caption += f"\n⭐ Rating: {update_data.get('vote_average', 'N/A')}\n"
                caption += f"📅 Year: {(update_data.get('release_date') or 'N/A')[:4]}\n"
                caption += f"🔊 Language: {update_data.get('language')}\n"
                caption += f"💿 Quality: {latest_file.get('quality')}\n"
                caption += f"📦 Size: {latest_file.get('size')}\n\n"
                
                home_link = WEBSITE_URL.rstrip('/')
                caption += f"🔗 *Download Now:* [Click Here]({home_link})"

                pub_keyboard = [
                    [{"text": "📥 Download / Watch Online", "url": home_link}],
                    [{"text": "📢 Join Our Channel", "url": f"https://t.me/{BOT_USERNAME}"}] 
                ]

                notify_payload = {
                    'chat_id': PUBLIC_CHANNEL_ID,
                    'parse_mode': 'Markdown',
                    'reply_markup': json.dumps({"inline_keyboard": pub_keyboard}),
                    'photo': new_poster,
                    'caption': caption
                }
                
                try:
                    resp = requests.post(f"{TELEGRAM_API_URL}/sendPhoto", json=notify_payload)
                    if resp.json().get('ok'):
                        now_utc = datetime.now(datetime.UTC) if hasattr(datetime, 'UTC') else datetime.utcnow()
                        movies.update_one({"_id": ObjectId(movie_id)}, {"$set": {"last_notified": now_utc}})
                except Exception as e:
                    print(f"❌ Failed to send late notification: {e}")

        return redirect(url_for('admin_home'))
        
    cat_list = list(categories.find())
    full_html = admin_base.replace('<!-- CONTENT_GOES_HERE -->', admin_edit)
    return render_template_string(full_html, movie=movie, categories=cat_list, active='dashboard')

@app.route('/admin/movie/delete/<movie_id>')
def admin_delete_movie(movie_id):
    if not check_auth(): return Response('Login Required', 401)
    movies.delete_one({"_id": ObjectId(movie_id)})
    return redirect(url_for('admin_home'))

@app.route('/admin/settings', methods=['GET', 'POST'])
def admin_settings_page():
    if not check_auth(): return Response('Login Required', 401)
    
    if request.method == 'POST':
        # YouTube Link Clean Logic
        raw_yt = request.form.get("tutorial_video")
        clean_yt = extract_youtube_id(raw_yt) if raw_yt else ""

        # Update Settings
        settings.update_one({}, {"$set": {
            "stealth_mode": request.form.get("stealth_mode") == "on",  # NEW: Stealth Toggle
            "shortener_domain": request.form.get("shortener_domain").strip(),
            "shortener_api": request.form.get("shortener_api").strip(),
            "tutorial_video_url": raw_yt, 
            "tutorial_video": clean_yt,   
            "banner_ad": request.form.get("banner_ad"),
            "popunder": request.form.get("popunder")
        }}, upsert=True)
        return redirect(url_for('admin_settings_page'))
    
    curr_settings = settings.find_one() or {}
    full_html = admin_base.replace('<!-- CONTENT_GOES_HERE -->', admin_settings)
    return render_template_string(full_html, settings=curr_settings, active='settings')

@app.route('/admin/api/tmdb')
def api_tmdb_search():
    if not check_auth(): return jsonify({'error': 'Unauthorized'}), 401
    query = request.args.get('q', '').strip()
    if not query or not TMDB_API_KEY: return jsonify({'error': 'No query provided'})

    tmdb_url_match = re.search(r'themoviedb\.org/(movie|tv)/(\d+)', query)
    if tmdb_url_match:
        m_type = tmdb_url_match.group(1) 
        m_id = tmdb_url_match.group(2)
        url = f"https://api.themoviedb.org/3/{m_type}/{m_id}?api_key={TMDB_API_KEY}"
        try:
            resp = requests.get(url)
            if resp.status_code == 200:
                data = resp.json()
                data['media_type'] = m_type
                return jsonify({'results': [data]})
        except: pass

    imdb_match = re.search(r'(tt\d+)', query)
    if imdb_match:
        imdb_id = imdb_match.group(1)
        url = f"https://api.themoviedb.org/3/find/{imdb_id}?api_key={TMDB_API_KEY}&external_source=imdb_id"
        try:
            data = requests.get(url).json()
            results = []
            if 'movie_results' in data and data['movie_results']: 
                for item in data['movie_results']: item['media_type'] = 'movie'
                results.extend(data['movie_results'])
            if 'tv_results' in data and data['tv_results']:
                for item in data['tv_results']: item['media_type'] = 'tv'
                results.extend(data['tv_results'])
            if results: return jsonify({'results': results})
        except: pass

    if query.isdigit():
        tmdb_id = query
        try:
            url_movie = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={TMDB_API_KEY}"
            resp = requests.get(url_movie)
            if resp.status_code == 200:
                data = resp.json()
                data['media_type'] = 'movie'
                return jsonify({'results': [data]})
            url_tv = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={TMDB_API_KEY}"
            resp = requests.get(url_tv)
            if resp.status_code == 200:
                data = resp.json()
                data['media_type'] = 'tv'
                return jsonify({'results': [data]})
        except: pass

    url = f"https://api.themoviedb.org/3/search/multi?api_key={TMDB_API_KEY}&query={requests.utils.quote(query)}"
    try:
        data = requests.get(url).json()
        return jsonify(data)
    except: return jsonify({'error': 'Search Failed'})

# --- START THREAD BEFORE APP RUN ---
# অ্যাপ রান হওয়ার আগে ব্যাকগ্রাউন্ড প্রসেস চালু করা
threading.Thread(target=start_scheduler, daemon=True).start()

if __name__ == '__main__':
    if WEBSITE_URL and BOT_TOKEN:
        hook_url = f"{WEBSITE_URL.rstrip('/')}/webhook/{BOT_TOKEN}"
        try: requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={hook_url}")
        except: pass

    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
