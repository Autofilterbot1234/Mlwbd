import os
import sys
import re
import requests
import json
from flask import Flask, render_template_string, request, redirect, url_for, Response, jsonify
from pymongo import MongoClient
from bson.objectid import ObjectId
from functools import wraps
from dotenv import load_dotenv
from datetime import datetime

# .env ফাইল থেকে এনভায়রনমেন্ট ভেরিয়েবল লোড করুন
load_dotenv()

app = Flask(__name__)

# --- Environment variables ---
MONGO_URI = os.getenv("MONGO_URI")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "password")

# --- Telegram & Channel Variables ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
PUBLIC_CHANNEL_ID = os.getenv("PUBLIC_CHANNEL_ID")
SOURCE_CHANNEL_ID = os.getenv("SOURCE_CHANNEL_ID") # ফাইল আপলোড করার সোর্স চ্যানেল
WEBSITE_URL = os.getenv("WEBSITE_URL")
MAIN_CHANNEL_LINK = os.getenv("MAIN_CHANNEL_LINK")
UPDATE_CHANNEL_LINK = os.getenv("UPDATE_CHANNEL_LINK")

# --- ভেরিয়েবল চেক করা ---
if not MONGO_URI or not BOT_TOKEN or not PUBLIC_CHANNEL_ID or not WEBSITE_URL:
    print("FATAL ERROR: MONGO_URI, BOT_TOKEN, PUBLIC_CHANNEL_ID, WEBSITE_URL must be set. Exiting.")
    sys.exit(1)

TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# --- ডেটাবেস কানেকশন ---
try:
    client = MongoClient(MONGO_URI)
    db = client["movie_db"]
    movies = db["movies"]
    settings = db["settings"]
    feedback = db["feedback"]
    print("Successfully connected to MongoDB!")
except Exception as e:
    print(f"Error connecting to MongoDB: {e}. Exiting.")
    sys.exit(1)

# === Context Processor ===
@app.context_processor
def inject_ads():
    ad_codes = settings.find_one()
    return dict(ad_settings=(ad_codes or {}))

# === Helper ফাংশন ===
def check_auth(username, password):
    return username == ADMIN_USERNAME and password == ADMIN_PASSWORD

def authenticate():
    return Response(
    'Could not verify your access level for that URL.\n'
    'You have to login with proper credentials', 401,
    {'WWW-Authenticate': 'Basic realm="Login Required"'})

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

def escape_markdown(text: str) -> str:
    if not isinstance(text, str): return ''
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def clean_filename(filename):
    """ফাইলের নাম ক্লিন করে মুভির নাম বের করা"""
    name = os.path.splitext(filename)[0]
    name = re.sub(r'(\d{4}|1080p|720p|480p|HEVC|x265|x264|WEB-DL|BluRay|HDTV|AAC|MKV|MP4|AVI|Hindi|Dubbed|Dual Audio)', '', name, flags=re.IGNORECASE)
    name = name.replace('.', ' ').replace('_', ' ')
    name = re.sub(r'\[.*?\]|\(.*?\)', '', name)
    return name.strip()

# === TMDB API Functions ===
def get_tmdb_details_by_title(title, content_type="movie"):
    if not TMDB_API_KEY: return {}
    tmdb_type = "tv" if content_type == "series" else "movie"
    try:
        search_url = f"https://api.themoviedb.org/3/search/{tmdb_type}?api_key={TMDB_API_KEY}&query={requests.utils.quote(title)}"
        search_res = requests.get(search_url, timeout=5).json()
        if not search_res.get("results"): return {}
        
        # প্রথম রেজাল্ট নেওয়া
        result = search_res["results"][0]
        tmdb_id = result.get("id")
        
        # বিস্তারিত তথ্য আনা
        detail_url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}?api_key={TMDB_API_KEY}"
        res = requests.get(detail_url, timeout=5).json()
        
        details = {
            "tmdb_id": tmdb_id,
            "title": res.get("name") if tmdb_type == "tv" else res.get("title"),
            "overview": res.get("overview"),
            "vote_average": res.get("vote_average")
        }
        if res.get("poster_path"): details["poster"] = f"https://image.tmdb.org/t/p/w500{res['poster_path']}"
        
        release_date = res.get("release_date") if tmdb_type == "movie" else res.get("first_air_date")
        if release_date: details["release_date"] = release_date
        
        if res.get("genres"): details["genres"] = [g['name'] for g in res.get("genres", [])]
        
        return details
    except Exception as e:
        print(f"TMDb API error: {e}")
        return {}

def get_trailer_key(tmdb_id, tmdb_type):
    if not TMDB_API_KEY or not tmdb_id: return None
    try:
        video_url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}/videos?api_key={TMDB_API_KEY}"
        video_res = requests.get(video_url, timeout=5).json()
        for v in video_res.get("results", []):
            if v['type'] == 'Trailer' and v['site'] == 'YouTube': return v['key']
    except: pass
    return None

# === চ্যানেল পোস্টিং ফাংশন (Website -> Public Channel) ===
def post_to_public_channel(content_id):
    try:
        content = movies.find_one({"_id": ObjectId(content_id)})
        if not content: return
        
        title = content.get('title', 'No Title')
        poster_url = content.get('poster')
        content_type = content.get('type', 'movie')
        
        escaped_title = escape_markdown(title)
        header_text = f"✨ *New Content Added\\!* ✨\n\n🎬 *{escaped_title}*"
        if content_type == 'series': header_text = f"📺 *New Series Added\\!* 📺\n\n🎬 *{escaped_title}*"
        
        caption = f"{header_text}\n"
        if content.get('release_date'): caption += f"\n🗓️ *Year:* {escape_markdown(str(content.get('release_date')).split('-')[0])}"
        if content.get('genres'): caption += f"\n🎭 *Genre:* {escape_markdown(', '.join(content.get('genres')))}"
        if content.get('vote_average'): caption += f"\n⭐ *Rating:* {escape_markdown(str(content.get('vote_average')))}/10"
        
        caption += f"\n\n*👇 Watch or Download Now 👇*"

        with app.app_context():
            website_link = f"{WEBSITE_URL.rstrip('/')}{url_for('movie_detail', movie_id=str(content_id))}"
        
        keyboard = {"inline_keyboard": [[{"text": "🍿 Watch on Website", "url": website_link}]]}
        if MAIN_CHANNEL_LINK: keyboard["inline_keyboard"].append([{"text": "📢 Join Main Channel", "url": MAIN_CHANNEL_LINK}])

        payload = {'chat_id': PUBLIC_CHANNEL_ID, 'parse_mode': 'MarkdownV2', 'reply_markup': json.dumps(keyboard)}
        if poster_url:
            payload.update({'photo': poster_url, 'caption': caption})
            requests.post(f"{TELEGRAM_API_URL}/sendPhoto", json=payload)
        else:
            payload.update({'text': caption})
            requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload)

    except Exception as e:
        print(f"Error posting to channel: {e}")

# === TELEGRAM WEBHOOK (Telegram Channel -> Website) ===
@app.route(f'/webhook/{BOT_TOKEN}', methods=['POST'])
def telegram_webhook():
    update = request.get_json()
    if not update or 'channel_post' not in update:
        return jsonify({'status': 'ignored'})
    
    msg = update['channel_post']
    chat_id = str(msg.get('chat', {}).get('id'))
    message_id = msg.get('message_id')
    
    # শুধুমাত্র নির্দিষ্ট সোর্স চ্যানেল থেকে রিসিভ করবে
    if SOURCE_CHANNEL_ID and chat_id != str(SOURCE_CHANNEL_ID):
        return jsonify({'status': 'wrong_channel'})

    # ফাইল ইনফো বের করা
    file_info = None
    original_filename = ""
    file_size_mb = 0
    
    if 'video' in msg:
        file_info = msg['video']
        original_filename = file_info.get('file_name', 'Unknown Video')
        file_size_mb = file_info.get('file_size', 0) / (1024 * 1024)
    elif 'document' in msg:
        file_info = msg['document']
        original_filename = file_info.get('file_name', 'Unknown Document')
        file_size_mb = file_info.get('file_size', 0) / (1024 * 1024)
    
    if not file_info: return jsonify({'status': 'no_file'})

    # টাইটেল প্রসেসিং
    raw_title = msg.get('caption') or clean_filename(original_filename)
    title = raw_title.strip()
    
    # সিরিজ না মুভি?
    content_type = "movie"
    if re.search(r'S\d+E\d+', original_filename, re.IGNORECASE) or re.search(r'Season \d+', title, re.IGNORECASE):
        content_type = "series"

    # TMDB থেকে ডিটেইলস আনা
    tmdb_data = get_tmdb_details_by_title(title, content_type)
    final_title = tmdb_data.get('title', title)
    
    # ফাইল লিংক তৈরি (Telegram Link)
    chat_username = msg.get('chat', {}).get('username')
    if chat_username:
        file_link = f"https://t.me/{chat_username}/{message_id}"
    else:
        clean_id = chat_id.replace("-100", "")
        file_link = f"https://t.me/c/{clean_id}/{message_id}"
        
    link_obj = {"quality": "Telegram File", "url": file_link, "size": f"{file_size_mb:.2f} MB"}

    # ডাটাবেসে সেভ বা আপডেট করা
    inserted_id = None
    try:
        existing = movies.find_one({"title": final_title})
        
        if existing:
            inserted_id = existing['_id']
            if content_type == "series":
                # এপিসোড নম্বর বের করা
                ep_match = re.search(r'S(\d+)E(\d+)', original_filename, re.IGNORECASE)
                ep_num = int(ep_match.group(2)) if ep_match else (len(existing.get('episodes', [])) + 1)
                
                # এপিসোড ডুপ্লিকেট চেক
                episode_exists = False
                if 'episodes' in existing:
                    for ep in existing['episodes']:
                        if ep['episode_number'] == ep_num:
                            episode_exists = True
                            break
                
                if not episode_exists:
                    new_episode = {
                        "episode_number": ep_num,
                        "title": f"Episode {ep_num}",
                        "links": [link_obj],
                        "watch_link": ""
                    }
                    movies.update_one({"_id": inserted_id}, {"$push": {"episodes": new_episode}})
            else:
                # মুভি হলে লিংক আপডেট করা যেতে পারে (অপশনাল)
                pass 
        else:
            # নতুন কন্টেন্ট তৈরি
            new_content = {
                "title": final_title,
                "type": content_type,
                "overview": tmdb_data.get('overview', 'No overview available.'),
                "poster": tmdb_data.get('poster'),
                "release_date": tmdb_data.get('release_date'),
                "genres": tmdb_data.get('genres', []),
                "vote_average": tmdb_data.get('vote_average'),
                "tmdb_id": tmdb_data.get('tmdb_id'),
                "is_trending": True,
                "is_coming_soon": False,
                "created_at": datetime.utcnow()
            }
            
            if content_type == "movie":
                new_content["links"] = [link_obj]
            else:
                ep_match = re.search(r'S(\d+)E(\d+)', original_filename, re.IGNORECASE)
                ep_num = int(ep_match.group(2)) if ep_match else 1
                new_content["episodes"] = [{
                    "episode_number": ep_num,
                    "title": f"Episode {ep_num}",
                    "links": [link_obj]
                }]
            
            res = movies.insert_one(new_content)
            inserted_id = res.inserted_id

        # টেলিগ্রাম পোস্টে বাটন যুক্ত করা
        if inserted_id:
            website_link = f"{WEBSITE_URL.rstrip('/')}{url_for('movie_detail', movie_id=str(inserted_id))}"
            edit_payload = {
                'chat_id': chat_id,
                'message_id': message_id,
                'reply_markup': json.dumps({
                    "inline_keyboard": [[{"text": "▶️ Watch / Download on Website", "url": website_link}]]
                })
            }
            requests.post(f"{TELEGRAM_API_URL}/editMessageReplyMarkup", json=edit_payload)

    except Exception as e:
        print(f"Webhook Error: {e}")

    return jsonify({'status': 'success'})

# --- HTML টেমপ্লেটসমূহ (আপনার আগের সম্পূর্ণ টেমপ্লেট) ---
# --- START OF index_html TEMPLATE ---
index_html = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" /><meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no" />
<title>MovieZone</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Roboto:wght@400;500;700&display=swap');
  :root { --netflix-red: #E50914; --netflix-black: #141414; --text-light: #f5f5f5; --text-dark: #a0a0a0; --nav-height: 60px; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Roboto', sans-serif; background-color: var(--netflix-black); color: var(--text-light); padding-bottom: 70px; }
  a { text-decoration: none; color: inherit; }
  .main-nav { position: fixed; top: 0; left: 0; width: 100%; padding: 15px 50px; display: flex; justify-content: space-between; align-items: center; z-index: 100; background: linear-gradient(to bottom, rgba(0,0,0,0.9) 0%, rgba(0,0,0,0)); }
  .main-nav.scrolled { background-color: var(--netflix-black); }
  .logo { font-family: 'Bebas Neue', sans-serif; font-size: 32px; color: var(--netflix-red); }
  .search-input { background: rgba(0,0,0,0.7); border: 1px solid #777; color: white; padding: 8px 15px; border-radius: 4px; }
  .hero-section { height: 70vh; position: relative; display: flex; align-items: flex-end; padding: 50px; background-size: cover; background-position: center; }
  .hero-section::before { content:''; position: absolute; inset: 0; background: linear-gradient(to top, var(--netflix-black), transparent); }
  .hero-content { position: relative; z-index: 2; max-width: 600px; }
  .hero-title { font-family: 'Bebas Neue'; font-size: 4rem; line-height: 1; margin-bottom: 10px; }
  .btn { padding: 10px 20px; border-radius: 4px; font-weight: bold; display: inline-flex; gap: 8px; align-items: center; }
  .btn-primary { background: var(--netflix-red); color: white; }
  .content-section { padding: 20px 50px; }
  .section-title { font-size: 1.5rem; margin-bottom: 20px; font-weight: 700; border-left: 4px solid var(--netflix-red); padding-left: 10px; }
  .movie-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 15px; }
  .movie-card { transition: transform 0.3s; }
  .movie-card:hover { transform: scale(1.05); }
  .poster { width: 100%; aspect-ratio: 2/3; object-fit: cover; border-radius: 4px; }
  .card-title { font-size: 0.9rem; margin-top: 5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .bottom-nav { position: fixed; bottom: 0; left: 0; right: 0; background: #181818; height: 60px; display: flex; justify-content: space-around; align-items: center; border-top: 1px solid #333; z-index: 200; }
  .nav-item { display: flex; flex-direction: column; align-items: center; color: #888; font-size: 12px; }
  .nav-item.active { color: var(--netflix-red); }
  .nav-item i { font-size: 20px; margin-bottom: 4px; }
  @media (max-width: 768px) {
      .main-nav { padding: 10px 20px; }
      .hero-section { height: 50vh; padding: 20px; }
      .hero-title { font-size: 2.5rem; }
      .content-section { padding: 20px; }
      .movie-grid { grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); }
  }
</style>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.2.0/css/all.min.css">
</head>
<body>
<header class="main-nav">
  <a href="{{ url_for('home') }}" class="logo">MovieZone</a>
  <form action="/"><input type="search" name="q" class="search-input" placeholder="Search..." value="{{ query|default('') }}" /></form>
</header>

{% if not is_full_page_list and recently_added %}
<div class="hero-section" style="background-image: url('{{ recently_added[0].poster }}');">
  <div class="hero-content">
    <h1 class="hero-title">{{ recently_added[0].title }}</h1>
    <p>{{ recently_added[0].overview[:150] }}...</p>
    <div style="margin-top: 20px;">
        <a href="{{ url_for('movie_detail', movie_id=recently_added[0]._id) }}" class="btn btn-primary"><i class="fas fa-play"></i> Watch Now</a>
    </div>
  </div>
</div>
{% endif %}

<main>
  {% if is_full_page_list %}
    <div class="content-section">
      <h2 class="section-title">{{ query }}</h2>
      <div class="movie-grid">
        {% for m in movies %}
        <a href="{{ url_for('movie_detail', movie_id=m._id) }}" class="movie-card">
            <div style="position:relative;">
                {% if m.poster_badge %}<span style="position:absolute; top:5px; left:5px; background:red; padding:2px 5px; font-size:10px; border-radius:3px;">{{ m.poster_badge }}</span>{% endif %}
                <img src="{{ m.poster or 'https://via.placeholder.com/200x300' }}" class="poster">
            </div>
            <h4 class="card-title">{{ m.title }}</h4>
        </a>
        {% endfor %}
      </div>
    </div>
  {% else %}
    <div class="content-section">
        {% if ad_settings.banner_ad_code %}<div style="margin:20px 0; text-align:center;">{{ ad_settings.banner_ad_code|safe }}</div>{% endif %}
        
        <h2 class="section-title">Latest Uploads</h2>
        <div class="movie-grid">
            {% for m in recently_added_full %}
            <a href="{{ url_for('movie_detail', movie_id=m._id) }}" class="movie-card">
                <img src="{{ m.poster or 'https://via.placeholder.com/200x300' }}" class="poster">
                <h4 class="card-title">{{ m.title }}</h4>
            </a>
            {% endfor %}
        </div>
    </div>
  {% endif %}
</main>

<nav class="bottom-nav">
  <a href="/" class="nav-item active"><i class="fas fa-home"></i><span>Home</span></a>
  <a href="/movies_only" class="nav-item"><i class="fas fa-film"></i><span>Movies</span></a>
  <a href="/webseries" class="nav-item"><i class="fas fa-tv"></i><span>Series</span></a>
  <a href="/contact" class="nav-item"><i class="fas fa-envelope"></i><span>Request</span></a>
</nav>

{% if ad_settings.popunder_code %}{{ ad_settings.popunder_code|safe }}{% endif %}
</body>
</html>
"""

# --- START OF detail_html TEMPLATE ---
detail_html = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" /><meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>{{ movie.title }}</title>
<style>
  :root { --netflix-red: #E50914; --bg: #141414; --text: #f5f5f5; }
  body { background: var(--bg); color: var(--text); font-family: sans-serif; padding: 20px; padding-bottom: 80px; }
  .container { max-width: 1000px; margin: 0 auto; }
  .detail-header { display: flex; gap: 30px; flex-wrap: wrap; margin-bottom: 30px; }
  .poster { width: 250px; border-radius: 8px; box-shadow: 0 5px 15px rgba(0,0,0,0.5); }
  .info { flex: 1; min-width: 300px; }
  h1 { font-size: 2.5rem; margin-bottom: 10px; color: var(--netflix-red); }
  .meta { color: #aaa; margin-bottom: 20px; font-size: 0.9rem; }
  .btn { display: block; background: #333; padding: 12px; margin-bottom: 10px; text-align: center; text-decoration: none; color: white; border-radius: 4px; font-weight: bold; }
  .btn-watch { background: var(--netflix-red); }
  .download-section { background: #1f1f1f; padding: 20px; border-radius: 8px; margin-top: 20px; }
  .episode { border-bottom: 1px solid #333; padding: 10px 0; display: flex; justify-content: space-between; align-items: center; }
  @media (max-width: 600px) { .detail-header { justify-content: center; text-align: center; } }
</style>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
</head>
<body>
<div class="container">
  <a href="/" style="color:#aaa;">&larr; Back to Home</a>
  <div class="detail-header">
    <img src="{{ movie.poster }}" class="poster">
    <div class="info">
      <h1>{{ movie.title }}</h1>
      <div class="meta">
        <span>⭐ {{ movie.vote_average }}</span> | <span>🗓️ {{ movie.release_date }}</span>
        <br><span>🎭 {{ movie.genres|join(', ') }}</span>
      </div>
      <p>{{ movie.overview }}</p>
      
      {% if movie.type == 'movie' and movie.watch_link %}
        <a href="{{ url_for('watch_movie', movie_id=movie._id) }}" class="btn btn-watch"><i class="fas fa-play"></i> Watch Online</a>
      {% endif %}
    </div>
  </div>

  {% if ad_settings.banner_ad_code %}<div style="text-align:center; margin: 20px 0;">{{ ad_settings.banner_ad_code|safe }}</div>{% endif %}

  <div class="download-section">
    {% if movie.type == 'movie' %}
        <h3>Download Links</h3>
        {% for link in movie.links %}
            <a href="{{ link.url }}" target="_blank" class="btn"><i class="fas fa-download"></i> Download {{ link.quality }} ({{ link.size }})</a>
        {% endfor %}
    {% else %}
        <h3>Episodes</h3>
        {% for ep in movie.episodes|sort(attribute='episode_number') %}
            <div class="episode">
                <span>{{ ep.title }}</span>
                <div>
                {% if ep.watch_link %}
                   <a href="{{ url_for('watch_movie', movie_id=movie._id, ep=ep.episode_number) }}" style="color:var(--netflix-red); margin-right:10px;"><i class="fas fa-play"></i></a>
                {% endif %}
                {% for link in ep.links %}
                    <a href="{{ link.url }}" target="_blank" style="color:#fff;"><i class="fas fa-download"></i></a>
                {% endfor %}
                </div>
            </div>
        {% endfor %}
    {% endif %}
  </div>
</div>
{% if ad_settings.popunder_code %}{{ ad_settings.popunder_code|safe }}{% endif %}
</body>
</html>
"""

# --- START OF admin_html TEMPLATE (Full Features) ---
admin_html = """
<!DOCTYPE html>
<html>
<head>
  <title>Admin Panel</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body { background: #141414; color: #fff; font-family: sans-serif; padding: 20px; }
    h2 { color: #E50914; }
    form { background: #222; padding: 20px; border-radius: 8px; margin-bottom: 30px; }
    input, textarea, select { width: 100%; padding: 10px; margin-bottom: 10px; background: #333; color: white; border: none; }
    button { background: #E50914; color: white; border: none; padding: 10px 20px; cursor: pointer; }
    table { width: 100%; border-collapse: collapse; margin-top: 20px; }
    th, td { border: 1px solid #444; padding: 10px; text-align: left; }
    .btn-edit { background: blue; padding: 5px 10px; text-decoration: none; color: white; }
    .btn-del { background: red; padding: 5px 10px; color: white; border: none; cursor: pointer; }
  </style>
</head>
<body>
  <h2>বিজ্ঞাপন পরিচালনা (Ad Management)</h2>
  <form action="{{ url_for('save_ads') }}" method="post">
    <label>Pop-Under / OnClick Ad Code</label><textarea name="popunder_code" rows="3">{{ ad_settings.popunder_code or '' }}</textarea>
    <label>Banner Ad Code</label><textarea name="banner_ad_code" rows="3">{{ ad_settings.banner_ad_code or '' }}</textarea>
    <button type="submit">Save Ad Codes</button>
  </form>

  <h2>Add New Content (Manual)</h2>
  <form method="post" action="{{ url_for('admin') }}">
    <input type="text" name="title" placeholder="Title (Required)" required />
    <select name="content_type"><option value="movie">Movie</option><option value="series">Series</option></select>
    <input type="text" name="poster_url" placeholder="Poster URL (Optional)" />
    <textarea name="overview" placeholder="Overview (Optional)"></textarea>
    <input type="text" name="watch_link" placeholder="Watch Link (Embed URL)" />
    <input type="text" name="link_720p" placeholder="Download Link" />
    <button type="submit">Add & Post to Channel</button>
  </form>

  <h2>Manage Content</h2>
  <table>
    <tr><th>Title</th><th>Type</th><th>Actions</th></tr>
    {% for movie in all_content %}
    <tr>
        <td>{{ movie.title }}</td>
        <td>{{ movie.type }}</td>
        <td>
            <a href="{{ url_for('edit_movie', movie_id=movie._id) }}" class="btn-edit">Edit</a>
            <a href="{{ url_for('delete_movie', movie_id=movie._id) }}" class="btn-del" onclick="return confirm('Delete?')">Delete</a>
        </td>
    </tr>
    {% endfor %}
  </table>
</body></html>
"""

# --- START OF edit_html TEMPLATE ---
edit_html = """
<!DOCTYPE html><html><head><title>Edit Movie</title><style>body{background:#141414;color:white;padding:20px;} form{max-width:600px;margin:0 auto;} input,textarea{width:100%;padding:10px;margin:10px 0;background:#333;color:white;border:none;} button{background:red;color:white;padding:10px;border:none;}</style></head>
<body>
  <h2>Edit Content</h2>
  <form method="post">
    <label>Title</label><input type="text" name="title" value="{{ movie.title }}">
    <label>Overview</label><textarea name="overview">{{ movie.overview }}</textarea>
    <label>Poster</label><input type="text" name="poster_url" value="{{ movie.poster }}">
    <label>Watch Link</label><input type="text" name="watch_link" value="{{ movie.watch_link or '' }}">
    <button type="submit">Update</button>
  </form>
</body></html>
"""

# --- START OF watch_html TEMPLATE ---
watch_html = """
<!DOCTYPE html><html><head><title>Watch</title><style>body{margin:0;background:black;display:flex;justify-content:center;align-items:center;height:100vh;} iframe{width:100%;height:100%;border:none;}</style></head><body>
<iframe src="{{ watch_link }}" allowfullscreen></iframe>
</body></html>
"""

# --- START OF contact_html TEMPLATE ---
contact_html = """
<!DOCTYPE html><html><body style="background:#141414;color:white;text-align:center;padding:50px;font-family:sans-serif;">
<h2>Contact / Request</h2>
<form method="post" style="max-width:400px;margin:0 auto;text-align:left;">
  <label>Your Message:</label><textarea name="message" style="width:100%;height:100px;margin:10px 0;"></textarea>
  <button type="submit" style="background:red;color:white;padding:10px 20px;border:none;">Send</button>
</form>
{% if message_sent %}<p style="color:green;">Message Sent!</p>{% endif %}
</body></html>
"""

# ----------------- Flask Routes -----------------

def fetch_and_prepare_data(form):
    title = form.get("title")
    content_type = form.get("content_type", "movie")
    movie_data = {
        "title": title, "type": content_type,
        "poster": form.get("poster_url", "").strip(),
        "overview": form.get("overview", "").strip(),
        "watch_link": form.get("watch_link", "").strip(),
        "is_trending": True, "created_at": datetime.utcnow()
    }
    
    # TMDB Auto Fetch if empty
    if not movie_data["poster"]:
        tmdb = get_tmdb_details_by_title(title, content_type)
        if tmdb:
            movie_data.update({k: v for k, v in tmdb.items() if v})

    # Manual Links
    link = form.get("link_720p")
    if link:
        dl_obj = {"quality": "Web-DL", "url": link}
        if content_type == "movie": movie_data["links"] = [dl_obj]
        else: movie_data["episodes"] = [{"episode_number": 1, "title": "Episode 1", "links": [dl_obj]}]
        
    return movie_data

def process_movie_list(movie_list):
    return [{**item, '_id': str(item['_id'])} for item in movie_list]

@app.route('/')
def home():
    query = request.args.get('q')
    if query:
        movies_list = list(movies.find({"title": {"$regex": query, "$options": "i"}}).sort('_id', -1))
        return render_template_string(index_html, movies=process_movie_list(movies_list), query=f'Search: "{query}"', is_full_page_list=True)
    
    limit = 12
    context = {
        "recently_added_full": process_movie_list(list(movies.find().sort('_id', -1).limit(limit))),
        "recently_added": process_movie_list(list(movies.find().sort('_id', -1).limit(1))),
        "is_full_page_list": False
    }
    return render_template_string(index_html, **context)

@app.route('/movies_only')
def movies_only():
    l = list(movies.find({"type": "movie"}).sort('_id', -1))
    return render_template_string(index_html, movies=process_movie_list(l), query="All Movies", is_full_page_list=True)

@app.route('/webseries')
def webseries():
    l = list(movies.find({"type": "series"}).sort('_id', -1))
    return render_template_string(index_html, movies=process_movie_list(l), query="Web Series", is_full_page_list=True)

@app.route('/movie/<movie_id>')
def movie_detail(movie_id):
    movie = movies.find_one({"_id": ObjectId(movie_id)})
    if not movie: return "Not Found", 404
    return render_template_string(detail_html, movie=movie)

@app.route('/watch/<movie_id>')
def watch_movie(movie_id):
    movie = movies.find_one({"_id": ObjectId(movie_id)})
    if not movie: return "Not Found", 404
    link = movie.get('watch_link')
    ep = request.args.get('ep')
    if ep and movie.get('episodes'):
        for e in movie['episodes']:
            if str(e['episode_number']) == ep:
                link = e.get('watch_link')
                break
    if not link: return "No watch link available", 404
    return render_template_string(watch_html, watch_link=link)

@app.route('/contact', methods=['GET', 'POST'])
def contact():
    msg_sent = False
    if request.method == 'POST':
        feedback.insert_one({"message": request.form.get("message"), "date": datetime.utcnow()})
        msg_sent = True
    return render_template_string(contact_html, message_sent=msg_sent)

# --- ADMIN ROUTES ---

@app.route('/admin', methods=["GET", "POST"])
@requires_auth
def admin():
    if request.method == "POST":
        data = fetch_and_prepare_data(request.form)
        result = movies.insert_one(data)
        if result.inserted_id:
            post_to_public_channel(result.inserted_id)
        return redirect(url_for('admin'))
    
    all_content = process_movie_list(list(movies.find().sort('_id', -1)))
    return render_template_string(admin_html, all_content=all_content)

@app.route('/admin/save_ads', methods=['POST'])
@requires_auth
def save_ads():
    ad_codes = { 
        "popunder_code": request.form.get("popunder_code", ""), 
        "banner_ad_code": request.form.get("banner_ad_code", "") 
    }
    settings.update_one({}, {"$set": ad_codes}, upsert=True)
    return redirect(url_for('admin'))

@app.route('/edit_movie/<movie_id>', methods=["GET", "POST"])
@requires_auth
def edit_movie(movie_id):
    if request.method == "POST":
        movies.update_one({"_id": ObjectId(movie_id)}, {"$set": {
            "title": request.form.get("title"),
            "overview": request.form.get("overview"),
            "poster": request.form.get("poster_url"),
            "watch_link": request.form.get("watch_link")
        }})
        return redirect(url_for('admin'))
    movie = movies.find_one({"_id": ObjectId(movie_id)})
    return render_template_string(edit_html, movie=movie)

@app.route('/delete_movie/<movie_id>')
@requires_auth
def delete_movie(movie_id):
    movies.delete_one({"_id": ObjectId(movie_id)})
    return redirect(url_for('admin'))

if __name__ == "__main__":
    # Webhook সেটআপ
    if WEBSITE_URL and BOT_TOKEN:
        hook_url = f"{WEBSITE_URL.rstrip('/')}/webhook/{BOT_TOKEN}"
        try:
            print(f"🔗 Setting Webhook: {hook_url}")
            requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={hook_url}")
        except: pass

    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
