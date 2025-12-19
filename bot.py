import os
import sys
import re
import requests
import json
import uuid
import math
from flask import Flask, render_template_string, request, redirect, url_for, Response, jsonify
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
SOURCE_CHANNEL_ID = os.getenv("SOURCE_CHANNEL_ID")
WEBSITE_URL = os.getenv("WEBSITE_URL")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# এডমিন ক্রেডেনশিয়াল
ADMIN_USER = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASSWORD", "admin")

# --- ডেটাবেস কানেকশন ---
try:
    client = MongoClient(MONGO_URI)
    db = client["moviezone_db"]
    movies = db["movies"]
    settings = db["settings"]
    print("✅ MongoDB Connected Successfully!")
except Exception as e:
    print(f"❌ MongoDB Connection Error: {e}")
    sys.exit(1)

# ================================
#        HELPER FUNCTIONS
# ================================

def clean_filename(filename):
    """
    ফাইলের নাম ক্লিন করে মেইন টাইটেল বের করে (Advanced Regex)।
    """
    # ১. এক্সটেনশন বাদ দেওয়া
    name = os.path.splitext(filename)[0]

    # ২. সব ধরণের সেপারেটর স্পেস দিয়ে রিপ্লেস করা
    name = re.sub(r'[._\-\+\[\]\(\)]', ' ', name)

    # ৩. স্টপ প্যাটার্ন (সাল, সিজন, রেজোলিউশন)
    stop_pattern = r'(\b(19|20)\d{2}\b|\bS\d+|\bSeason|\bEp?\s*\d+|\b480p|\b720p|\b1080p|\b2160p|\bHD|\bWeb-?dl|\bBluray|\bDual|\bHindi|\bBangla)'
    
    match = re.search(stop_pattern, name, re.IGNORECASE)
    if match:
        name = name[:match.start()]

    # ৪. অতিরিক্ত স্পেস রিমুভ
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
    
    # হাই প্রায়োরিটি কিওয়ার্ড
    if re.search(r'\b(multi|multi audio)\b', text): return "Multi Audio"
    if re.search(r'\b(dual|dual audio)\b', text): detected.append("Dual Audio")
    
    lang_map = {
        'Bengali': ['bengali', 'bangla', 'ben'],
        'Hindi': ['hindi', 'hin'],
        'English': ['english', 'eng'],
        'Tamil': ['tamil', 'tam'],
        'Telugu': ['telugu', 'tel'],
        'Korean': ['korean', 'kor'],
        'Japanese': ['japanese', 'jap'],
        'Spanish': ['spanish', 'spa'],
        'French': ['french', 'fre']
    }
    
    for lang_name, keywords in lang_map.items():
        pattern = r'\b(' + '|'.join(keywords) + r')\b'
        if re.search(pattern, text): detected.append(lang_name)
    
    if not detected: return "English"
    return " + ".join(list(dict.fromkeys(detected)))

def get_episode_label(filename):
    label = ""
    season = ""
    
    # সিজন খোঁজা
    match_s = re.search(r'\b(S|Season)\s*(\d+)', filename, re.IGNORECASE)
    if match_s: season = f"S{int(match_s.group(2)):02d}"

    # রেঞ্জ খোঁজা (E01-E05)
    match_range = re.search(r'E(\d+)\s*-\s*E?(\d+)', filename, re.IGNORECASE)
    if match_range:
        return (f"{season} E{int(match_range.group(1)):02d}-{int(match_range.group(2)):02d}").strip()

    # সাধারণ S01E01
    match_se = re.search(r'\bS(\d+)\s*E(\d+)\b', filename, re.IGNORECASE)
    if match_se: return f"S{int(match_se.group(1)):02d} E{int(match_se.group(2)):02d}"
    
    # শুধু এপিসোড
    match_ep = re.search(r'\b(Episode|Ep|E)\s*(\d+)\b', filename, re.IGNORECASE)
    if match_ep:
        ep_num = int(match_ep.group(2))
        if ep_num < 1900: return f"{season} Episode {ep_num}".strip()
    
    if season: return f"Season {int(match_s.group(2))}"
    return None

def get_tmdb_details(title, content_type="movie", year=None):
    """
    TMDB থেকে Cast, Genre, Trailer সহ বিস্তারিত তথ্য আনে।
    """
    if not TMDB_API_KEY: return {"title": title}
    tmdb_type = "tv" if content_type == "series" else "movie"
    try:
        # ১. সার্চ করে ID বের করা
        query_str = requests.utils.quote(title)
        search_url = f"https://api.themoviedb.org/3/search/{tmdb_type}?api_key={TMDB_API_KEY}&query={query_str}"
        if year and tmdb_type == "movie": search_url += f"&year={year}"

        search_res = requests.get(search_url, timeout=5).json()
        
        if search_res.get("results"):
            res = search_res["results"][0]
            tmdb_id = res["id"]

            # ২. বিস্তারিত তথ্য (Cast, Videos) আনা
            details_url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}?api_key={TMDB_API_KEY}&append_to_response=credits,videos"
            details = requests.get(details_url, timeout=5).json()

            # ট্রেলার
            trailer = None
            for vid in details.get("videos", {}).get("results", []):
                if vid["site"] == "YouTube" and vid["type"] == "Trailer":
                    trailer = f"https://www.youtube.com/watch?v={vid['key']}"
                    break
            
            # কাস্ট ও জেনরা
            cast = [c["name"] for c in details.get("credits", {}).get("cast", [])[:5]]
            genres = [g["name"] for g in details.get("genres", [])[:3]]

            poster = f"https://image.tmdb.org/t/p/w500{details.get('poster_path')}" if details.get('poster_path') else None
            backdrop = f"https://image.tmdb.org/t/p/w1280{details.get('backdrop_path')}" if details.get('backdrop_path') else None

            return {
                "tmdb_id": tmdb_id,
                "title": details.get("name") if tmdb_type == "tv" else details.get("title"),
                "overview": details.get("overview"),
                "poster": poster,
                "backdrop": backdrop,
                "release_date": details.get("first_air_date") if tmdb_type == "tv" else details.get("release_date"),
                "vote_average": details.get("vote_average"),
                "genres": genres,
                "cast": cast,
                "trailer": trailer
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

# === Context Processor ===
@app.context_processor
def inject_globals():
    ad_codes = settings.find_one() or {}
    return dict(ad_settings=ad_codes, BOT_USERNAME=BOT_USERNAME, site_name="MovieZone")

# ================================
#        TELEGRAM WEBHOOK
# ================================
@app.route(f'/webhook/{BOT_TOKEN}', methods=['POST'])
def telegram_webhook():
    update = request.get_json()
    if not update: return jsonify({'status': 'ignored'})

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
        
        # 1. Clean Title & Extract Year
        search_title = clean_filename(raw_input) 
        year_match = re.search(r'\b(19|20)\d{2}\b', raw_input)
        search_year = year_match.group(0) if year_match else None
        
        # 2. Determine Type
        content_type = "movie"
        if re.search(r'(S\d+|Season|Episode|Ep\s*\d+|Combined|E\d+-E\d+)', file_name, re.IGNORECASE) or re.search(r'(S\d+|Season)', str(raw_caption), re.IGNORECASE):
            content_type = "series"

        # 3. Get Full Details
        tmdb_data = get_tmdb_details(search_title, content_type, search_year)
        final_title = tmdb_data.get('title', search_title)
        quality = get_file_quality(file_name)
        
        episode_label = get_episode_label(file_name)
        if content_type == "series" and not episode_label:
            clean_part = file_name.replace(search_title, "").replace(".", " ").strip()
            if len(clean_part) > 3: episode_label = clean_part[:25]

        language = detect_language(raw_input)
        unique_code = str(uuid.uuid4())[:8]

        file_obj = {
            "file_id": file_id,
            "unique_code": unique_code,
            "filename": file_name,
            "quality": quality,
            "episode_label": episode_label,
            "size": f"{file_size_mb:.2f} MB",
            "file_type": file_type,
            "added_at": datetime.utcnow()
        }

        existing_movie = movies.find_one({"title": final_title})
        movie_id = None
        should_notify = False

        if existing_movie:
            if content_type == "series" and episode_label:
                is_duplicate = False
                for f in existing_movie.get('files', []):
                    if f.get('episode_label') == episode_label and f.get('quality') == quality:
                        is_duplicate = True
                        break
                should_notify = not is_duplicate
            else:
                should_notify = False

            movies.update_one(
                {"_id": existing_movie['_id']},
                {"$push": {"files": file_obj}, "$set": {"updated_at": datetime.utcnow()}}
            )
            movie_id = existing_movie['_id']
        else:
            should_notify = True
            new_movie = {
                "title": final_title,
                "overview": tmdb_data.get('overview'),
                "poster": tmdb_data.get('poster'),
                "backdrop": tmdb_data.get('backdrop'),
                "release_date": tmdb_data.get('release_date'),
                "vote_average": tmdb_data.get('vote_average'),
                "genres": tmdb_data.get('genres', []),
                "cast": tmdb_data.get('cast', []),
                "trailer": tmdb_data.get('trailer'),
                "language": language,
                "type": content_type,
                "files": [file_obj],
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow()
            }
            res = movies.insert_one(new_movie)
            movie_id = res.inserted_id

        # --- Notification ---
        if movie_id and WEBSITE_URL:
            dl_link = f"{WEBSITE_URL.rstrip('/')}/movie/{str(movie_id)}"
            
            # Edit Source Message
            try:
                requests.post(f"{TELEGRAM_API_URL}/editMessageReplyMarkup", json={
                    'chat_id': chat_id,
                    'message_id': msg['message_id'],
                    'reply_markup': json.dumps({"inline_keyboard": [[{"text": "▶️ Download from Website", "url": dl_link}]]})
                })
            except: pass

            # Public Channel Post
            if PUBLIC_CHANNEL_ID and should_notify:
                notify_caption = f"🎬 *{escape_markdown(final_title)}*\n"
                if episode_label: notify_caption += f"📌 {escape_markdown(episode_label)}\n"
                
                notify_caption += f"\n⭐ Rating: {tmdb_data.get('vote_average', 'N/A')}\n"
                notify_caption += f"📅 Year: {(tmdb_data.get('release_date') or 'N/A')[:4]}\n"
                notify_caption += f"🔊 Language: {language}\n"
                notify_caption += f"💿 Quality: {quality}\n"
                notify_caption += f"📦 Size: {file_size_mb:.2f} MB\n\n"
                notify_caption += f"🔗 *Download Now:* [Click Here]({dl_link})"

                notify_payload = {
                    'chat_id': PUBLIC_CHANNEL_ID,
                    'parse_mode': 'Markdown',
                    'reply_markup': json.dumps({"inline_keyboard": [[{"text": "📥 Download / Watch Online", "url": dl_link}]]})
                }

                if tmdb_data.get('poster'):
                    notify_payload['photo'] = tmdb_data.get('poster')
                    notify_payload['caption'] = notify_caption
                    try: requests.post(f"{TELEGRAM_API_URL}/sendPhoto", json=notify_payload)
                    except: pass
                else:
                    notify_payload['text'] = notify_caption
                    try: requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=notify_payload)
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
                        caption += f"🔊 Audio: {movie.get('language', 'N/A')}\n"
                        caption += f"💿 Quality: {target_file['quality']}\n"
                        caption += f"📦 Size: {target_file['size']}\n\n"
                        caption += f"✅ *Downloaded from {escape_markdown(WEBSITE_URL)}*"
                        
                        payload = {'chat_id': chat_id, 'caption': caption, 'parse_mode': 'Markdown'}
                        method = 'sendVideo' if target_file['file_type'] == 'video' else 'sendDocument'
                        if target_file['file_type'] == 'video': payload['video'] = target_file['file_id']
                        else: payload['document'] = target_file['file_id']
                        requests.post(f"{TELEGRAM_API_URL}/{method}", json=payload)
                    else:
                        requests.post(f"{TELEGRAM_API_URL}/sendMessage", json={'chat_id': chat_id, 'text': "❌ File expired."})
                else:
                    requests.post(f"{TELEGRAM_API_URL}/sendMessage", json={'chat_id': chat_id, 'text': "❌ Invalid Link."})
            else:
                requests.post(f"{TELEGRAM_API_URL}/sendMessage", json={'chat_id': chat_id, 'text': "👋 Welcome! Use the website to download movies."})

    return jsonify({'status': 'ok'})

# ================================
#        FRONTEND TEMPLATES
# ================================

index_template = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ site_name }} - Stream & Download</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;700&display=swap');
        :root { --primary: #e50914; --dark: #141414; --gray: #2f2f2f; --text: #fff; }
        body { background-color: var(--dark); color: var(--text); font-family: 'Outfit', sans-serif; margin: 0; padding-bottom: 80px; }
        a { text-decoration: none; color: inherit; transition: 0.3s; }
        
        .navbar { display: flex; justify-content: space-between; align-items: center; padding: 15px 25px; background: rgba(20,20,20,0.95); position: sticky; top: 0; z-index: 100; backdrop-filter: blur(10px); border-bottom: 1px solid #333; }
        .logo { font-size: 24px; font-weight: 800; color: var(--primary); letter-spacing: 1px; }
        .search-box { position: relative; background: #000; border: 1px solid #333; padding: 8px 15px; border-radius: 50px; display: flex; align-items: center; }
        .search-box input { background: transparent; border: none; color: #fff; outline: none; width: 150px; font-size: 14px; }
        
        .hero { height: 50vh; min-height: 400px; background-size: cover; background-position: center; position: relative; display: flex; align-items: center; margin-bottom: 30px; }
        .hero::before { content: ''; position: absolute; inset: 0; background: linear-gradient(90deg, #141414 0%, rgba(20,20,20,0.6) 50%, transparent 100%); }
        .hero::after { content: ''; position: absolute; bottom: 0; left: 0; right: 0; height: 100px; background: linear-gradient(to top, var(--dark), transparent); }
        .hero-content { position: relative; z-index: 2; padding-left: 5%; max-width: 600px; }
        .hero-title { font-size: 3rem; font-weight: 800; line-height: 1.1; margin-bottom: 15px; text-shadow: 2px 2px 10px rgba(0,0,0,0.8); }
        .hero-meta { display: flex; gap: 15px; font-size: 0.9rem; color: #ccc; margin-bottom: 20px; font-weight: 500; }
        .btn-hero { background: var(--primary); color: #fff; padding: 12px 30px; border-radius: 4px; font-weight: 600; display: inline-flex; align-items: center; gap: 10px; font-size: 1rem; }
        .btn-hero:hover { transform: scale(1.05); box-shadow: 0 5px 15px rgba(229,9,20,0.4); }

        .container { padding: 0 5%; }
        .section-title { font-size: 1.4rem; font-weight: 600; margin-bottom: 20px; border-left: 4px solid var(--primary); padding-left: 15px; display: flex; align-items: center; gap: 10px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 15px; }
        @media (min-width: 768px) { .grid { grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 20px; } }

        .card { position: relative; border-radius: 8px; overflow: hidden; transition: transform 0.3s ease; background: var(--gray); aspect-ratio: 2/3; cursor: pointer; }
        .card:hover { transform: translateY(-5px); box-shadow: 0 10px 20px rgba(0,0,0,0.5); z-index: 2; }
        .card img { width: 100%; height: 100%; object-fit: cover; opacity: 0.9; transition: 0.3s; }
        .card:hover img { opacity: 1; transform: scale(1.05); }
        .card-overlay { position: absolute; inset: 0; background: linear-gradient(to top, rgba(0,0,0,0.95), transparent 60%); display: flex; flex-direction: column; justify-content: flex-end; padding: 10px; opacity: 0.9; }
        .card-title { font-size: 0.9rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 3px; }
        .card-info { font-size: 0.75rem; color: #bbb; display: flex; justify-content: space-between; }
        .badge-hd { position: absolute; top: 10px; left: 10px; background: rgba(0,0,0,0.7); color: #fff; padding: 2px 6px; font-size: 10px; border-radius: 3px; border: 1px solid #fff; font-weight: bold; }
        .rating { position: absolute; top: 10px; right: 10px; background: var(--primary); color: #fff; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold; }

        .pagination { display: flex; justify-content: center; margin-top: 40px; gap: 10px; }
        .page-btn { background: #222; color: #fff; padding: 10px 15px; border-radius: 4px; border: 1px solid #333; }
        .page-btn.active { background: var(--primary); border-color: var(--primary); }
        .page-btn:hover:not(.active) { background: #333; }

        .bottom-nav { position: fixed; bottom: 0; left:0; width: 100%; background: rgba(20,20,20,0.95); backdrop-filter: blur(10px); border-top: 1px solid #333; display: flex; justify-content: space-around; padding: 12px 0; z-index: 1000; }
        .nav-item { text-align: center; font-size: 10px; color: #888; }
        .nav-item i { font-size: 20px; margin-bottom: 5px; display: block; }
        .nav-item.active { color: var(--primary); }
    </style>
</head>
<body>

<nav class="navbar">
    <a href="/" class="logo">{{ site_name }}</a>
    <form action="/" method="GET" class="search-box">
        <input type="text" name="q" placeholder="Search..." value="{{ query }}">
        <i class="fas fa-search" style="color:#777"></i>
    </form>
</nav>

{% if not query and featured and page == 1 %}
<div class="hero" style="background-image: url('{{ featured.backdrop or featured.poster }}');">
    <div class="hero-content">
        <h1 class="hero-title">{{ featured.title }}</h1>
        <div class="hero-meta">
            <span><i class="fas fa-star" style="color:#e50914"></i> {{ featured.vote_average }}</span>
            <span>{{ (featured.release_date or 'N/A')[:4] }}</span>
            <span>{{ featured.language }}</span>
            {% if featured.genres %}<span>{{ featured.genres[0] }}</span>{% endif %}
        </div>
        <p style="color: #ddd; font-size: 0.95rem; line-height: 1.5; margin-bottom: 20px; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;">
            {{ featured.overview }}
        </p>
        <a href="{{ url_for('movie_detail', movie_id=featured._id) }}" class="btn-hero">
            <i class="fas fa-play"></i> Watch Now
        </a>
    </div>
</div>
{% endif %}

<div class="container">
    {% if ad_settings.banner_ad %}<div style="margin-bottom:20px; text-align:center;">{{ ad_settings.banner_ad|safe }}</div>{% endif %}

    <h2 class="section-title">
        {% if query %} Search Results: "{{ query }}" {% else %} Latest Releases {% endif %}
    </h2>

    <div class="grid">
        {% for movie in movies %}
        <a href="{{ url_for('movie_detail', movie_id=movie._id) }}" class="card">
            <span class="badge-hd">HD</span>
            <span class="rating">{{ movie.vote_average }}</span>
            <img src="{{ movie.poster or 'https://via.placeholder.com/300x450?text=No+Poster' }}" loading="lazy">
            <div class="card-overlay">
                <div class="card-title">{{ movie.title }}</div>
                <div class="card-info">
                    <span>{{ (movie.release_date or 'N/A')[:4] }}</span>
                    <span>{{ movie.type|capitalize }}</span>
                </div>
            </div>
        </a>
        {% endfor %}
    </div>

    {% if total_pages > 1 %}
    <div class="pagination">
        {% if page > 1 %}
        <a href="?page={{ page-1 }}&q={{ query }}" class="page-btn"><i class="fas fa-chevron-left"></i></a>
        {% endif %}
        <span class="page-btn active">{{ page }}</span>
        {% if page < total_pages %}
        <a href="?page={{ page+1 }}&q={{ query }}" class="page-btn"><i class="fas fa-chevron-right"></i></a>
        {% endif %}
    </div>
    {% endif %}
    
    <div style="height: 30px;"></div>
</div>

<div class="bottom-nav">
    <a href="/" class="nav-item {{ 'active' if not request.args.get('type') else '' }}"><i class="fas fa-home"></i>Home</a>
    <a href="/movies" class="nav-item {{ 'active' if request.args.get('type') == 'movie' else '' }}"><i class="fas fa-film"></i>Movies</a>
    <a href="/series" class="nav-item {{ 'active' if request.args.get('type') == 'series' else '' }}"><i class="fas fa-tv"></i>Series</a>
</div>

{% if ad_settings.popunder %}{{ ad_settings.popunder|safe }}{% endif %}
</body>
</html>
"""

detail_template = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ movie.title }} - Download</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap');
        :root { --primary: #e50914; --dark: #141414; --gray: #1f1f1f; --text: #eee; }
        body { background-color: var(--dark); color: var(--text); font-family: 'Outfit', sans-serif; margin: 0; padding-bottom: 40px; }
        
        .backdrop-container { position: relative; height: 50vh; min-height: 350px; }
        .backdrop { width: 100%; height: 100%; object-fit: cover; mask-image: linear-gradient(to bottom, black 20%, transparent 100%); opacity: 0.5; }
        .back-nav { position: absolute; top: 20px; left: 20px; z-index: 10; display: flex; align-items: center; gap: 10px; cursor: pointer; color: #fff; font-weight: 600; text-shadow: 0 2px 4px rgba(0,0,0,0.8); }
        
        .content { max-width: 1000px; margin: -150px auto 0; padding: 20px; position: relative; z-index: 5; display: flex; flex-direction: column; gap: 30px; }
        
        .header { display: flex; gap: 30px; align-items: flex-end; }
        .poster { width: 160px; border-radius: 8px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); border: 2px solid rgba(255,255,255,0.1); }
        
        .info h1 { margin: 0 0 10px; font-size: 2.2rem; line-height: 1.1; }
        .meta { display: flex; flex-wrap: wrap; gap: 10px; font-size: 0.9rem; color: #ccc; margin-bottom: 15px; }
        .tag { background: rgba(255,255,255,0.1); padding: 4px 10px; border-radius: 4px; border: 1px solid rgba(255,255,255,0.1); }
        .rating-star { color: #ffd700; margin-right: 5px; }
        
        .actions { display: flex; gap: 15px; margin-top: 15px; }
        .btn-trailer { background: #fff; color: #000; padding: 10px 20px; border-radius: 4px; font-weight: 600; display: inline-flex; align-items: center; gap: 8px; transition: 0.3s; }
        .btn-trailer:hover { background: #ddd; }
        
        .overview { background: var(--gray); padding: 20px; border-radius: 8px; line-height: 1.6; color: #ddd; border: 1px solid #333; }
        .cast-list { display: flex; gap: 10px; overflow-x: auto; padding-bottom: 10px; margin-top: 5px; }
        .cast-badge { background: #222; padding: 5px 12px; border-radius: 20px; white-space: nowrap; font-size: 0.85rem; border: 1px solid #333; color: #aaa; }
        
        .download-sec { margin-top: 20px; }
        .sec-title { font-size: 1.2rem; border-left: 4px solid var(--primary); padding-left: 10px; margin-bottom: 20px; color: #fff; }
        
        .file-card { background: var(--gray); border-radius: 6px; padding: 15px; margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center; border: 1px solid #333; transition: 0.3s; }
        .file-card:hover { border-color: #555; background: #252525; }
        .file-info { display: flex; flex-direction: column; gap: 4px; }
        .f-title { color: #fff; font-weight: 600; font-size: 1rem; }
        .f-meta { font-size: 0.8rem; color: #888; }
        .btn-dl { background: var(--primary); color: #fff; padding: 8px 20px; border-radius: 4px; font-weight: 600; display: flex; align-items: center; gap: 8px; font-size: 0.9rem; }
        .btn-dl:hover { background: #f40612; }

        @media (max-width: 768px) {
            .header { flex-direction: column; align-items: center; text-align: center; }
            .content { margin-top: -100px; }
            .poster { width: 140px; margin-bottom: 10px; }
            .meta { justify-content: center; }
            .actions { justify-content: center; }
        }
    </style>
</head>
<body>

<a href="/" class="back-nav"><i class="fas fa-arrow-left"></i> Back to Home</a>

<div class="backdrop-container">
    <img src="{{ movie.backdrop or movie.poster }}" class="backdrop">
</div>

<div class="content">
    <div class="header">
        <img src="{{ movie.poster }}" class="poster">
        <div class="info">
            <h1>{{ movie.title }}</h1>
            
            <div class="meta">
                <span class="tag"><i class="fas fa-star rating-star"></i> {{ movie.vote_average }}</span>
                <span class="tag">{{ (movie.release_date or 'N/A')[:4] }}</span>
                <span class="tag">{{ movie.type|upper }}</span>
                <span class="tag" style="background:var(--primary); border:none;">{{ movie.language }}</span>
            </div>

            {% if movie.genres %}
            <div style="font-size: 0.9rem; color: #aaa; margin-bottom: 10px;">
                {{ movie.genres|join(', ') }}
            </div>
            {% endif %}

            <div class="actions">
                {% if movie.trailer %}
                <a href="{{ movie.trailer }}" target="_blank" class="btn-trailer">
                    <i class="fab fa-youtube" style="color:red;"></i> Trailer
                </a>
                {% endif %}
                <button onclick="document.getElementById('dl-sec').scrollIntoView({behavior: 'smooth'})" class="btn-trailer" style="background: var(--primary); color:white;">
                    <i class="fas fa-download"></i> Download
                </button>
            </div>
        </div>
    </div>

    {% if movie.cast %}
    <div>
        <h3 style="font-size:1rem; color:#aaa; margin-bottom:8px;">Starring</h3>
        <div class="cast-list">
            {% for actor in movie.cast %}
            <span class="cast-badge">{{ actor }}</span>
            {% endfor %}
        </div>
    </div>
    {% endif %}

    <div class="overview">
        <h3 style="margin-top:0; color:#fff;">Storyline</h3>
        {{ movie.overview }}
    </div>

    {% if ad_settings.banner_ad %}<div style="text-align:center;">{{ ad_settings.banner_ad|safe }}</div>{% endif %}

    <div class="download-sec" id="dl-sec">
        <h3 class="sec-title">Download Links</h3>
        {% if movie.files %}
            {% for file in movie.files|reverse %}
            <div class="file-card">
                <div class="file-info">
                    <div class="f-title">
                        {% if file.episode_label %}
                            <span style="color:#ffd700">{{ file.episode_label }}</span>
                        {% else %}
                            {{ file.quality }}
                        {% endif %}
                    </div>
                    <div class="f-meta">
                        {{ file.size }} • {{ file.file_type|upper }} • {{ file.filename[:30] }}...
                    </div>
                </div>
                <a href="https://t.me/{{ BOT_USERNAME }}?start={{ file.unique_code }}" class="btn-dl">
                    <i class="fab fa-telegram-plane"></i> Get File
                </a>
            </div>
            {% endfor %}
        {% else %}
            <p style="text-align: center; color: #666;">No files added yet.</p>
        {% endif %}
    </div>

    <div style="text-align: center; margin-top: 30px; font-size: 0.8rem; color: #555;">
        &copy; {{ site_name }} 2025 • <a href="#">DMCA</a>
    </div>
</div>

{% if ad_settings.popunder %}{{ ad_settings.popunder|safe }}{% endif %}

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
    <a href="/admin/settings" class="{{ 'active' if active == 'settings' else '' }}"><i class="fas fa-cogs"></i> <span>Settings</span></a>
    <a href="/" target="_blank"><i class="fas fa-external-link-alt"></i> <span>View Site</span></a>
</div>

<div class="main-content">
    <!-- CONTENT_GOES_HERE -->
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
<script>
    function searchTMDB() {
        const query = document.getElementById('tmdbQuery').value;
        const resultDiv = document.getElementById('tmdbResults');
        if(!query) return;
        
        resultDiv.innerHTML = '<div class="text-info">Searching...</div>';
        
        fetch('/admin/api/tmdb?q=' + encodeURIComponent(query))
        .then(r => r.json())
        .then(data => {
            if(data.error) {
                resultDiv.innerHTML = '<div class="text-danger">'+data.error+'</div>';
                return;
            }
            let html = '<div class="list-group mt-2">';
            data.results.forEach(item => {
                let title = item.title || item.name;
                let date = item.release_date || item.first_air_date || 'N/A';
                let itemStr = JSON.stringify(item).replace(/'/g, "&#39;");
                
                html += `<button type="button" class="list-group-item list-group-item-action d-flex align-items-center gap-3" onclick='fillForm(${itemStr})'>
                    <img src="https://image.tmdb.org/t/p/w92${item.poster_path}" style="width:40px; border-radius:4px;">
                    <div>
                        <div class="fw-bold">${title}</div>
                        <small class="text-muted">${date.substring(0,4)}</small>
                    </div>
                </button>`;
            });
            html += '</div>';
            resultDiv.innerHTML = html;
        });
    }

    function fillForm(data) {
        document.querySelector('input[name="title"]').value = data.title || data.name;
        document.querySelector('textarea[name="overview"]').value = data.overview;
        document.querySelector('input[name="poster"]').value = 'https://image.tmdb.org/t/p/w500' + data.poster_path;
        document.querySelector('input[name="backdrop"]').value = 'https://image.tmdb.org/t/p/w1280' + data.backdrop_path;
        document.querySelector('input[name="release_date"]').value = data.release_date || data.first_air_date;
        document.querySelector('input[name="vote_average"]').value = data.vote_average;
        document.querySelector('.poster-preview').src = 'https://image.tmdb.org/t/p/w500' + data.poster_path;
        document.getElementById('tmdbResults').innerHTML = '<div class="text-success">Data Applied! Click Update to Save.</div>';
    }
</script>
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
                        <p class="card-text small text-muted mb-1">{{ (movie.release_date or '')[:4] }} • {{ movie.language }}</p>
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

admin_edit = """
<div class="container-fluid">
    <div class="d-flex justify-content-between align-items-center mb-4">
        <h3>Edit Movie: <span class="text-primary">{{ movie.title }}</span></h3>
        <a href="/admin" class="btn btn-secondary btn-sm">Back</a>
    </div>

    <div class="row">
        <!-- TMDB Search Column -->
        <div class="col-md-4 mb-4">
            <div class="card p-3">
                <h5>Fetch Data from TMDB</h5>
                <div class="input-group mb-3">
                    <input type="text" id="tmdbQuery" class="form-control" placeholder="Enter Movie Name..." value="{{ movie.title }}">
                    <button class="btn btn-warning" type="button" onclick="searchTMDB()">Search</button>
                </div>
                <div id="tmdbResults" style="max-height: 400px; overflow-y: auto;"></div>
            </div>
            
            <div class="card p-3 mt-3 text-center">
                <label class="form-label">Current Poster</label><br>
                <img src="{{ movie.poster }}" class="poster-preview">
            </div>
        </div>

        <!-- Edit Form Column -->
        <div class="col-md-8">
            <div class="card p-4">
                <form method="POST">
                    <div class="row">
                        <div class="col-md-8 mb-3">
                            <label class="form-label">Movie Title</label>
                            <input type="text" name="title" class="form-control" value="{{ movie.title }}" required>
                        </div>
                        <div class="col-md-4 mb-3">
                            <label class="form-label">Language (Badge)</label>
                            <input type="text" name="language" class="form-control" value="{{ movie.language }}" placeholder="e.g. Hindi, Dual Audio">
                        </div>
                    </div>

                    <div class="mb-3">
                        <label class="form-label">Overview / Story</label>
                        <textarea name="overview" class="form-control" rows="4">{{ movie.overview }}</textarea>
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
                            <label class="form-label">TMDB Rating</label>
                            <input type="text" name="vote_average" class="form-control" value="{{ movie.vote_average }}">
                        </div>
                        <div class="col-md-4 mb-3">
                            <label class="form-label">Type</label>
                            <select name="type" class="form-control">
                                <option value="movie" {{ 'selected' if movie.type == 'movie' else '' }}>Movie</option>
                                <option value="series" {{ 'selected' if movie.type == 'series' else '' }}>Series</option>
                            </select>
                        </div>
                    </div>

                    <button type="submit" class="btn btn-primary btn-lg w-100">Update Movie Details</button>
                </form>
            </div>
        </div>
    </div>
</div>
"""

admin_settings = """
<div class="container" style="max-width: 800px;">
    <h3 class="mb-4">Website Settings</h3>
    <div class="card p-4">
        <form method="POST">
            <div class="mb-4">
                <label class="form-label fw-bold text-warning">Banner Ad Code (HTML)</label>
                <div class="form-text mb-2">This code appears on the Homepage and Download Page.</div>
                <textarea name="banner_ad" class="form-control" rows="5" style="font-family: monospace;">{{ settings.banner_ad }}</textarea>
            </div>
            
            <div class="mb-4">
                <label class="form-label fw-bold text-warning">Popunder / Scripts (HTML/JS)</label>
                <div class="form-text mb-2">Use this for Popunders, Google Analytics, or other hidden scripts.</div>
                <textarea name="popunder" class="form-control" rows="5" style="font-family: monospace;">{{ settings.popunder }}</textarea>
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
    query = request.args.get('q', '').strip()
    filter_type = request.args.get('type')
    page = int(request.args.get('page', 1))
    per_page = 24
    
    db_query = {}
    if query: db_query["title"] = {"$regex": query, "$options": "i"}
    if filter_type: db_query["type"] = filter_type

    total_movies = movies.count_documents(db_query)
    total_pages = math.ceil(total_movies / per_page)

    movie_list = list(movies.find(db_query).sort([('updated_at', -1), ('_id', -1)]).skip((page - 1) * per_page).limit(per_page))
    
    featured = None
    if not query and not filter_type and page == 1 and movie_list:
        featured = movie_list[0] 

    return render_template_string(index_template, movies=movie_list, query=query, featured=featured, page=page, total_pages=total_pages)

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
        if not movie: return "Movie Not Found", 404
        return render_template_string(detail_template, movie=movie)
    except:
        return "Invalid ID", 400

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

@app.route('/admin/movie/edit/<movie_id>', methods=['GET', 'POST'])
def admin_edit_movie(movie_id):
    if not check_auth(): return Response('Login Required', 401)
    
    if request.method == 'POST':
        update_data = {
            "title": request.form.get("title"),
            "language": request.form.get("language"),
            "overview": request.form.get("overview"),
            "poster": request.form.get("poster"),
            "backdrop": request.form.get("backdrop"),
            "release_date": request.form.get("release_date"),
            "vote_average": request.form.get("vote_average"),
            "type": request.form.get("type"),
            "updated_at": datetime.utcnow()
        }
        movies.update_one({"_id": ObjectId(movie_id)}, {"$set": update_data})
        return redirect(url_for('admin_home'))
        
    movie = movies.find_one({"_id": ObjectId(movie_id)})
    
    full_html = admin_base.replace('<!-- CONTENT_GOES_HERE -->', admin_edit)
    return render_template_string(full_html, movie=movie, active='dashboard')

@app.route('/admin/movie/delete/<movie_id>')
def admin_delete_movie(movie_id):
    if not check_auth(): return Response('Login Required', 401)
    movies.delete_one({"_id": ObjectId(movie_id)})
    return redirect(url_for('admin_home'))

@app.route('/admin/settings', methods=['GET', 'POST'])
def admin_settings_page():
    if not check_auth(): return Response('Login Required', 401)
    
    if request.method == 'POST':
        settings.update_one({}, {"$set": {
            "banner_ad": request.form.get("banner_ad"),
            "popunder": request.form.get("popunder")
        }}, upsert=True)
        return redirect(url_for('admin_settings_page'))
    
    curr_settings = settings.find_one() or {}
    
    full_html = admin_base.replace('<!-- CONTENT_GOES_HERE -->', admin_settings)
    return render_template_string(full_html, settings=curr_settings, active='settings')

# API for Admin Panel (JS Fetch)
@app.route('/admin/api/tmdb')
def api_tmdb_search():
    if not check_auth(): return jsonify({'error': 'Unauthorized'}), 401
    query = request.args.get('q')
    if not query or not TMDB_API_KEY: return jsonify({'error': 'No query or API key'})
    
    url = f"https://api.themoviedb.org/3/search/multi?api_key={TMDB_API_KEY}&query={requests.utils.quote(query)}"
    try:
        data = requests.get(url).json()
        return jsonify(data)
    except:
        return jsonify({'error': 'TMDB Request Failed'})

if __name__ == '__main__':
    # Webhook Auto-Set
    if WEBSITE_URL and BOT_TOKEN:
        hook_url = f"{WEBSITE_URL.rstrip('/')}/webhook/{BOT_TOKEN}"
        try: requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={hook_url}")
        except: pass

    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
