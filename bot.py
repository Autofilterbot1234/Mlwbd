import os
import sys
import re
import requests
import json
import uuid
from flask import Flask, render_template_string, request, redirect, url_for, Response, jsonify
from pymongo import MongoClient
from bson.objectid import ObjectId
from functools import wraps
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

# === Helper Functions (SMART CLEANER ADDED) ===

def clean_filename(filename):
    """
    ফাইলের নাম থেকে সব আবর্জনা ফেলে শুধু মুভির নাম বের করার স্মার্ট ফাংশন।
    উদাহরণ: "Pathaan.2023.1080p.WEB-DL.mkv" -> "Pathaan"
    """
    # ১. এক্সটেনশন বাদ দেওয়া
    name = os.path.splitext(filename)[0]
    
    # ২. ডট, আন্ডারস্কোর, ব্র্যাকেট স্পেস দিয়ে রিপ্লেস করা
    name = re.sub(r'[._\-\[\]\(\)]', ' ', name)
    
    # ৩. '2023', '1999', '1080p', 'S01' দেখলেই তার পরের সব কেটে ফেলা (Cut-off logic)
    # এই Regex টি বছর (19xx/20xx) অথবা কোয়ালিটি অথবা সিজন পেলেই থামবে
    match = re.search(r'(\b(19|20)\d{2}\b|\b(?:480|720|1080|2160)[pP]\b|S\d+E\d+|Season)', name, re.IGNORECASE)
    
    if match:
        # প্যাটার্ন মিললে তার আগেরটুকু নিবে
        name = name[:match.start()]
    
    # ৪. অতিরিক্ত স্পেস বা কমন আবর্জনা ক্লিন করা
    junk_words = r'\b(hindi|dual|audio|dubbed|sub|esub|web-dl|bluray|rip|x264|hevc)\b'
    name = re.sub(junk_words, '', name, flags=re.IGNORECASE)
    
    return name.strip()

def get_file_quality(filename):
    """ফাইলের নাম থেকে কোয়ালিটি ডিটেক্ট করা (Display এর জন্য)"""
    filename = filename.lower()
    if "4k" in filename or "2160p" in filename: return "4K UHD"
    if "1080p" in filename: return "1080p Full HD"
    if "720p" in filename: return "720p HD"
    if "480p" in filename: return "480p SD"
    return "High Quality"

def get_tmdb_details(title, content_type="movie"):
    """TMDB থেকে মুভি ডিটেইলস এবং পোস্টার আনা"""
    if not TMDB_API_KEY: return {"title": title}
    
    tmdb_type = "tv" if content_type == "series" else "movie"
    try:
        # ক্লিন করা টাইটেল দিয়ে সার্চ
        search_url = f"https://api.themoviedb.org/3/search/{tmdb_type}?api_key={TMDB_API_KEY}&query={requests.utils.quote(title)}"
        data = requests.get(search_url, timeout=5).json()
        
        if data.get("results"):
            res = data["results"][0]
            # হাই-কোয়ালিটি পোস্টার লিংক তৈরি
            poster = f"https://image.tmdb.org/t/p/w780{res['poster_path']}" if res.get('poster_path') else None
            backdrop = f"https://image.tmdb.org/t/p/w1280{res['backdrop_path']}" if res.get('backdrop_path') else None
            
            return {
                "tmdb_id": res.get("id"),
                "title": res.get("name") if tmdb_type == "tv" else res.get("title"),
                "overview": res.get("overview"),
                "poster": poster,
                "backdrop": backdrop,
                "release_date": res.get("first_air_date") if tmdb_type == "tv" else res.get("release_date"),
                "vote_average": res.get("vote_average")
            }
    except Exception as e:
        print(f"TMDB Error: {e}")
    
    return {"title": title} # ফেইল করলে অন্তত টাইটেল রিটার্ন করবে

def escape_markdown(text):
    """টেলিগ্রাম মার্কডাউনের জন্য টেক্সট এস্কেপ করা"""
    if not text: return ""
    chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(chars)}])', r'\\\1', text)

# === Context Processor (Global Variables) ===
@app.context_processor
def inject_globals():
    ad_codes = settings.find_one() or {}
    return dict(
        ad_settings=ad_codes,
        BOT_USERNAME=BOT_USERNAME,
        site_name="MovieZone"
    )

# === TELEGRAM WEBHOOK HANDLER ===
@app.route(f'/webhook/{BOT_TOKEN}', methods=['POST'])
def telegram_webhook():
    update = request.get_json()
    if not update: return jsonify({'status': 'ignored'})

    # 1. CHANNEL POST HANDLING (ফাইল আপলোড লজিক)
    if 'channel_post' in update:
        msg = update['channel_post']
        chat_id = str(msg.get('chat', {}).get('id'))

        # শুধুমাত্র সোর্স চ্যানেল থেকে রিসিভ করবে
        if SOURCE_CHANNEL_ID and chat_id != str(SOURCE_CHANNEL_ID):
            return jsonify({'status': 'wrong_channel'})

        # ফাইল ইনফো বের করা
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

        # --- SMART PROCESSING HERE ---
        raw_caption = msg.get('caption')
        # যদি ক্যাপশন থাকে সেটা নিব, না হলে ফাইলের নাম নিব
        raw_input = raw_caption if raw_caption else file_name
        
        # ১. ক্লিনার দিয়ে শুধু নাম বের করা (TMDB এর জন্য)
        search_title = clean_filename(raw_input)
        
        # ২. সিরিজ নাকি মুভি চেক
        content_type = "movie"
        if re.search(r'(S\d+|Season)', file_name, re.IGNORECASE) or re.search(r'(S\d+|Season)', str(raw_caption), re.IGNORECASE):
            content_type = "series"

        # ৩. TMDB থেকে ডাটা আনা
        tmdb_data = get_tmdb_details(search_title, content_type)
        final_title = tmdb_data.get('title', search_title) # TMDB না পেলে ক্লিন করা নামই ব্যবহার হবে
        
        # ৪. ডিসপ্লের জন্য কোয়ালিটি
        quality = get_file_quality(file_name)

        # ৫. ইউনিক কোড তৈরি
        unique_code = str(uuid.uuid4())[:8]

        file_obj = {
            "file_id": file_id,
            "unique_code": unique_code,
            "filename": file_name, # অরিজিনাল নাম রাখা হলো যাতে ইউজার বুঝে কি ডাউনলোড করছে
            "quality": quality,
            "size": f"{file_size_mb:.2f} MB",
            "file_type": file_type,
            "added_at": datetime.utcnow()
        }

        # ৬. ডাটাবেসে সেভ করা
        existing_movie = movies.find_one({"title": final_title})

        if existing_movie:
            movies.update_one(
                {"_id": existing_movie['_id']},
                {"$push": {"files": file_obj}, "$set": {"updated_at": datetime.utcnow()}}
            )
            movie_id = existing_movie['_id']
        else:
            new_movie = {
                "title": final_title,
                "overview": tmdb_data.get('overview'),
                "poster": tmdb_data.get('poster'),
                "backdrop": tmdb_data.get('backdrop'),
                "release_date": tmdb_data.get('release_date'),
                "vote_average": tmdb_data.get('vote_average'),
                "genres": ["Action", "Drama"], 
                "type": content_type,
                "files": [file_obj],
                "created_at": datetime.utcnow()
            }
            res = movies.insert_one(new_movie)
            movie_id = res.inserted_id

        # ৭. টেলিগ্রাম চ্যানেলে এডিট করে বাটন বসানো
        if movie_id and WEBSITE_URL:
            dl_link = f"{WEBSITE_URL.rstrip('/')}/movie/{str(movie_id)}"
            edit_payload = {
                'chat_id': chat_id,
                'message_id': msg['message_id'],
                'reply_markup': json.dumps({
                    "inline_keyboard": [[
                        {"text": "▶️ Download from Website", "url": dl_link}
                    ]]
                })
            }
            try:
                requests.post(f"{TELEGRAM_API_URL}/editMessageReplyMarkup", json=edit_payload)
            except: pass

        return jsonify({'status': 'success', 'search_term': search_title, 'matched_title': final_title})

    # 2. BOT PRIVATE MESSAGE HANDLING
    elif 'message' in update:
        msg = update['message']
        chat_id = msg.get('chat', {}).get('id')
        text = msg.get('text', '')

        if text.startswith('/start'):
            parts = text.split()
            if len(parts) > 1:
                code = parts[1]
                
                # কোড দিয়ে মুভি এবং ফাইল খোঁজা
                movie = movies.find_one({"files.unique_code": code})
                
                if movie:
                    target_file = next((f for f in movie['files'] if f['unique_code'] == code), None)
                    if target_file:
                        caption = f"🎬 *{escape_markdown(movie['title'])}*\n"
                        if movie.get('vote_average'): caption += f"⭐ Rating: {movie['vote_average']}\n"
                        caption += f"💿 *{target_file['quality']}*\n"
                        caption += f"📦 Size: {target_file['size']}\n\n"
                        caption += f"✅ *Downloaded from {escape_markdown(WEBSITE_URL)}*"

                        payload = {'chat_id': chat_id, 'caption': caption, 'parse_mode': 'Markdown'}
                        
                        method = 'sendVideo' if target_file['file_type'] == 'video' else 'sendDocument'
                        
                        if target_file['file_type'] == 'video':
                            payload['video'] = target_file['file_id']
                        else:
                            payload['document'] = target_file['file_id']

                        requests.post(f"{TELEGRAM_API_URL}/{method}", json=payload)
                    else:
                        requests.post(f"{TELEGRAM_API_URL}/sendMessage", json={'chat_id': chat_id, 'text': "❌ File expired."})
                else:
                    requests.post(f"{TELEGRAM_API_URL}/sendMessage", json={'chat_id': chat_id, 'text': "❌ Invalid Link."})
            else:
                requests.post(f"{TELEGRAM_API_URL}/sendMessage", json={'chat_id': chat_id, 'text': "👋 Welcome! Use the website to get files."})

    return jsonify({'status': 'ok'})

# ================================
#        FRONTEND (TEMPLATES)
# ================================

index_template = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ site_name }} - Watch Movies</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600;700&display=swap');
        :root { --primary: #E50914; --dark: #141414; --gray: #2F2F2F; --text: #fff; --text-sec: #b3b3b3; }
        * { margin: 0; padding: 0; box-sizing: border-box; font-family: 'Poppins', sans-serif; }
        body { background-color: var(--dark); color: var(--text); padding-bottom: 60px; }
        a { text-decoration: none; color: inherit; }
        
        .navbar { display: flex; justify-content: space-between; align-items: center; padding: 15px 5%; background: linear-gradient(180deg, rgba(0,0,0,0.7) 10%, transparent); position: fixed; width: 100%; top: 0; z-index: 100; transition: 0.3s; }
        .navbar.scrolled { background-color: var(--dark); box-shadow: 0 2px 10px rgba(0,0,0,0.5); }
        .logo { font-size: 24px; font-weight: 700; color: var(--primary); letter-spacing: 1px; }
        .search-box input { background: rgba(0,0,0,0.6); border: 1px solid #fff; padding: 8px 15px; border-radius: 20px; color: #fff; outline: none; width: 150px; transition: 0.3s; }
        .search-box input:focus { background: rgba(0,0,0,0.9); width: 220px; border-color: var(--primary); }

        .hero { height: 70vh; background-size: cover; background-position: center; position: relative; display: flex; align-items: flex-end; }
        .hero::before { content: ''; position: absolute; inset: 0; background: linear-gradient(to top, var(--dark) 10%, transparent 90%); }
        .hero-content { position: relative; z-index: 2; padding: 0 5% 40px; max-width: 600px; }
        .hero-title { font-size: 3rem; margin-bottom: 10px; line-height: 1.1; }
        .hero-meta { color: var(--text-sec); margin-bottom: 20px; font-size: 0.9rem; }
        .btn { display: inline-flex; align-items: center; gap: 8px; padding: 10px 25px; border-radius: 4px; font-weight: 600; cursor: pointer; border: none; }
        .btn-primary { background: var(--primary); color: #fff; }

        .section { padding: 40px 5%; }
        .section-title { font-size: 1.4rem; margin-bottom: 20px; border-left: 4px solid var(--primary); padding-left: 10px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 20px; }
        .card { transition: transform 0.3s; position: relative; cursor: pointer; }
        .card:hover { transform: scale(1.05); z-index: 10; }
        .card-img { width: 100%; aspect-ratio: 2/3; object-fit: cover; border-radius: 6px; }
        .card-info { padding-top: 8px; }
        .card-title { font-size: 0.9rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-weight: 500; }
        .rating-badge { position: absolute; top: 10px; right: 10px; background: rgba(0,0,0,0.8); color: #ffb400; padding: 2px 6px; border-radius: 4px; font-size: 12px; font-weight: bold; }

        .bottom-nav { position: fixed; bottom: 0; width: 100%; background: #1a1a1a; display: flex; justify-content: space-around; padding: 10px 0; border-top: 1px solid #333; z-index: 1000; }
        .nav-item { display: flex; flex-direction: column; align-items: center; color: var(--text-sec); font-size: 10px; }
        .nav-item i { font-size: 18px; margin-bottom: 4px; }
        .nav-item.active { color: var(--primary); }
        .ad-banner { margin: 20px 0; text-align: center; overflow: hidden; }

        @media (max-width: 768px) {
            .hero { height: 50vh; }
            .hero-title { font-size: 2rem; }
            .navbar { background: var(--dark); }
        }
    </style>
</head>
<body>

<nav class="navbar">
    <a href="/" class="logo">{{ site_name }}</a>
    <form action="/" method="GET" class="search-box">
        <input type="text" name="q" placeholder="Search..." value="{{ query }}">
    </form>
</nav>

{% if not query and featured %}
<header class="hero" style="background-image: url('{{ featured.backdrop or featured.poster }}');">
    <div class="hero-content">
        <h1 class="hero-title">{{ featured.title }}</h1>
        <div class="hero-meta">
            <span>⭐ {{ featured.vote_average }}</span> • 
            <span>{{ (featured.release_date or 'N/A')[:4] }}</span>
        </div>
        <p style="color:#ddd; margin-bottom:20px; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;">{{ featured.overview }}</p>
        <a href="{{ url_for('movie_detail', movie_id=featured._id) }}" class="btn btn-primary"><i class="fas fa-play"></i> Watch Now</a>
    </div>
</header>
{% endif %}

<main class="section">
    {% if ad_settings.banner_ad %}<div class="ad-banner">{{ ad_settings.banner_ad|safe }}</div>{% endif %}

    <h2 class="section-title">{{ query and 'Search Results' or 'Latest Uploads' }}</h2>
    <div class="grid">
        {% for movie in movies %}
        <a href="{{ url_for('movie_detail', movie_id=movie._id) }}" class="card">
            <span class="rating-badge">{{ movie.vote_average }}</span>
            <img src="{{ movie.poster or 'https://via.placeholder.com/300x450?text=No+Poster' }}" class="card-img" loading="lazy">
            <div class="card-info">
                <h3 class="card-title">{{ movie.title }}</h3>
            </div>
        </a>
        {% endfor %}
    </div>
</main>

<nav class="bottom-nav">
    <a href="/" class="nav-item active"><i class="fas fa-home"></i>Home</a>
    <a href="/movies" class="nav-item"><i class="fas fa-film"></i>Movies</a>
    <a href="/series" class="nav-item"><i class="fas fa-tv"></i>Series</a>
</nav>

{% if ad_settings.popunder %}{{ ad_settings.popunder|safe }}{% endif %}

<script>
    window.addEventListener('scroll', () => {
        document.querySelector('.navbar').classList.toggle('scrolled', window.scrollY > 50);
    });
</script>
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
        @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600;700&display=swap');
        :root { --primary: #E50914; --dark: #141414; --gray: #222; --text: #eee; }
        body { background-color: var(--dark); color: var(--text); font-family: 'Poppins', sans-serif; padding-top: 70px; padding-bottom: 50px; }
        .container { max-width: 1100px; margin: 0 auto; padding: 20px; }
        
        .movie-header { display: flex; gap: 40px; flex-wrap: wrap; }
        .poster-wrapper { flex: 0 0 280px; }
        .poster { width: 100%; border-radius: 8px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); }
        .info { flex: 1; min-width: 300px; }
        
        h1 { font-size: 2.5rem; color: #fff; margin-bottom: 10px; line-height: 1.2; }
        .meta { color: #aaa; font-size: 0.9rem; margin-bottom: 20px; display: flex; gap: 15px; align-items: center; }
        .tag { border: 1px solid #444; padding: 2px 8px; border-radius: 4px; }
        .overview { line-height: 1.6; color: #ccc; margin-bottom: 30px; }
        
        .download-box { background: var(--gray); border-radius: 10px; padding: 25px; margin-top: 30px; border: 1px solid #333; }
        .box-title { margin-bottom: 20px; font-size: 1.2rem; border-bottom: 1px solid #444; padding-bottom: 10px; }
        
        .file-list { display: flex; flex-direction: column; gap: 15px; }
        .file-item { display: flex; justify-content: space-between; align-items: center; background: #2a2a2a; padding: 15px; border-radius: 6px; transition: 0.2s; }
        .file-item:hover { background: #333; }
        
        .file-info h4 { font-size: 1rem; color: #fff; margin-bottom: 5px; }
        .file-info span { font-size: 0.8rem; color: #888; }
        
        .btn-dl { background: #0088cc; color: white; padding: 10px 20px; border-radius: 4px; text-decoration: none; font-weight: 600; font-size: 0.9rem; display: flex; align-items: center; gap: 8px; }
        .btn-dl:hover { background: #0077b5; }
        
        @media (max-width: 768px) {
            .movie-header { flex-direction: column; }
            .poster-wrapper { width: 100%; max-width: 250px; margin: 0 auto; }
            h1 { font-size: 1.8rem; text-align: center; }
            .meta { justify-content: center; }
            .file-item { flex-direction: column; gap: 15px; text-align: center; }
            .btn-dl { width: 100%; justify-content: center; }
        }
    </style>
</head>
<body>

<div class="container">
    <a href="/" style="color: #888; font-size: 0.9rem;"><i class="fas fa-arrow-left"></i> Back to Home</a>
    
    <div class="movie-header" style="margin-top: 20px;">
        <div class="poster-wrapper">
            <img src="{{ movie.poster or 'https://via.placeholder.com/300x450' }}" class="poster">
        </div>
        <div class="info">
            <h1>{{ movie.title }}</h1>
            <div class="meta">
                <span style="color: #ffb400;"><i class="fas fa-star"></i> {{ movie.vote_average }}</span>
                <span class="tag">{{ (movie.release_date or 'N/A')[:4] }}</span>
                <span class="tag">{{ movie.type|upper }}</span>
            </div>
            <p class="overview">{{ movie.overview }}</p>
            
            {% if ad_settings.banner_ad %}<div style="margin: 20px 0;">{{ ad_settings.banner_ad|safe }}</div>{% endif %}

            <div class="download-box">
                <h3 class="box-title"><i class="fas fa-download"></i> Download / Watch Files</h3>
                <div class="file-list">
                    {% if movie.files %}
                        {% for file in movie.files|reverse %}
                        <div class="file-item">
                            <div class="file-info">
                                <h4>{{ file.filename }}</h4>
                                <span><i class="fas fa-sd-card"></i> {{ file.size }} • <i class="fas fa-video"></i> {{ file.quality }}</span>
                            </div>
                            <a href="https://t.me/{{ BOT_USERNAME }}?start={{ file.unique_code }}" target="_blank" class="btn-dl">
                                <i class="fab fa-telegram-plane"></i> Get File
                            </a>
                        </div>
                        {% endfor %}
                    {% else %}
                        <p style="text-align: center; color: #777;">No files uploaded yet.</p>
                    {% endif %}
                </div>
            </div>
            
            <p style="margin-top: 15px; font-size: 0.8rem; color: #666; text-align: center;">
                * Clicking 'Get File' will open Telegram Bot. Click 'Start' to receive the file instantly.
            </p>

        </div>
    </div>
</div>

{% if ad_settings.popunder %}{{ ad_settings.popunder|safe }}{% endif %}

</body>
</html>
"""

# ================================
#        FLASK ROUTES
# ================================

@app.route('/')
def home():
    query = request.args.get('q', '').strip()
    filter_type = request.args.get('type')
    
    db_query = {}
    if query:
        db_query["title"] = {"$regex": query, "$options": "i"}
    if filter_type:
        db_query["type"] = filter_type

    # Sort by updated_at first to show newly added files on top
    movie_list = list(movies.find(db_query).sort([('updated_at', -1), ('_id', -1)]).limit(24))
    
    featured = None
    if not query and not filter_type and movie_list:
        featured = movie_list[0] 

    return render_template_string(index_template, movies=movie_list, query=query, featured=featured)

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

# --- Admin Panel ---
@app.route('/admin', methods=['GET', 'POST'])
def admin():
    auth = request.authorization
    # Admin Auth check via Environment variables
    admin_user = os.getenv("ADMIN_USERNAME", "admin")
    admin_pass = os.getenv("ADMIN_PASSWORD", "admin")
    
    if not auth or not (auth.username == admin_user and auth.password == admin_pass):
        return Response('Login Required', 401, {'WWW-Authenticate': 'Basic realm="Login Required"'})

    if request.method == 'POST':
        settings.update_one({}, {"$set": {
            "banner_ad": request.form.get("banner_ad"),
            "popunder": request.form.get("popunder")
        }}, upsert=True)
        return "Settings Saved! <a href='/admin'>Back</a>"
    
    current_settings = settings.find_one() or {}
    
    return f"""
    <h1>Admin Panel</h1>
    <form method="POST">
        <h3>Banner Ad Code (HTML)</h3>
        <textarea name="banner_ad" style="width:100%; height:100px;">{current_settings.get('banner_ad', '')}</textarea><br>
        <h3>Popunder / Script Code</h3>
        <textarea name="popunder" style="width:100%; height:100px;">{current_settings.get('popunder', '')}</textarea><br><br>
        <button type="submit">Save Settings</button>
    </form>
    """

if __name__ == '__main__':
    # Webhook Auto-Set
    if WEBSITE_URL and BOT_TOKEN:
        hook_url = f"{WEBSITE_URL.rstrip('/')}/webhook/{BOT_TOKEN}"
        try:
            requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={hook_url}")
        except: pass

    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
