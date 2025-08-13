import os
import sys
import re
import requests
import json
from flask import Flask, render_template_string, request, redirect, url_for, Response, jsonify
from pymongo import MongoClient
from bson.objectid import ObjectId
from functools import wraps
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

# ======================================================================
# --- আপনার ব্যক্তিগত ও অ্যাডমিন তথ্য (এনভায়রনমেন্ট থেকে লোড হবে) ---
# ======================================================================
MONGO_URI = os.environ.get("MONGO_URI")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
ADMIN_CHANNEL_ID = os.environ.get("ADMIN_CHANNEL_ID")
BOT_USERNAME = os.environ.get("BOT_USERNAME")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

# আপনার চ্যানেল এবং ডেভেলপারের তথ্য
MAIN_CHANNEL_LINK = os.environ.get("MAIN_CHANNEL_LINK")
UPDATE_CHANNEL_LINK = os.environ.get("UPDATE_CHANNEL_LINK")
DEVELOPER_USER_LINK = os.environ.get("DEVELOPER_USER_LINK")

# নোটিফিকেশন চ্যানেলের আইডি
NOTIFICATION_CHANNEL_ID = os.environ.get("NOTIFICATION_CHANNEL_ID")

# --- প্রয়োজনীয় ভেরিয়েবলগুলো সেট করা হয়েছে কিনা তা পরীক্ষা করা ---
required_vars = {
    "MONGO_URI": MONGO_URI, "BOT_TOKEN": BOT_TOKEN, "TMDB_API_KEY": TMDB_API_KEY,
    "ADMIN_CHANNEL_ID": ADMIN_CHANNEL_ID, "BOT_USERNAME": BOT_USERNAME,
    "ADMIN_USERNAME": ADMIN_USERNAME, "ADMIN_PASSWORD": ADMIN_PASSWORD,
    "MAIN_CHANNEL_LINK": MAIN_CHANNEL_LINK,
    "UPDATE_CHANNEL_LINK": UPDATE_CHANNEL_LINK,
    "DEVELOPER_USER_LINK": DEVELOPER_USER_LINK,
    "NOTIFICATION_CHANNEL_ID": NOTIFICATION_CHANNEL_ID
}

missing_vars = [name for name, value in required_vars.items() if not value]
if missing_vars:
    print(f"FATAL: Missing required environment variables: {', '.join(missing_vars)}")
    print("Please set these variables in your deployment environment and restart the application.")
    sys.exit(1)

# ======================================================================
# --- অ্যাপ্লিকেশন সেটআপ এবং অন্যান্য ফাংশন ---
# ======================================================================
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
PLACEHOLDER_POSTER = "https://via.placeholder.com/400x600.png?text=Poster+Not+Found"
app = Flask(__name__)

def check_auth(username, password):
    return username == ADMIN_USERNAME and password == ADMIN_PASSWORD

def authenticate():
    return Response('Could not verify your access level.', 401, {'WWW-Authenticate': 'Basic realm="Login Required"'})

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

try:
    client = MongoClient(MONGO_URI)
    db = client["movie_db"]
    movies = db["movies"]
    settings = db["settings"]
    feedback = db["feedback"]
    print("SUCCESS: Successfully connected to MongoDB!")
except Exception as e:
    print(f"FATAL: Error connecting to MongoDB: {e}. Exiting.")
    sys.exit(1)

@app.context_processor
def inject_ads():
    ad_codes = settings.find_one()
    return dict(ad_settings=(ad_codes or {}), bot_username=BOT_USERNAME, main_channel_link=MAIN_CHANNEL_LINK)

def delete_message_after_delay(chat_id, message_id):
    print(f"Attempting to delete message {message_id} from chat {chat_id}")
    try:
        url = f"{TELEGRAM_API_URL}/deleteMessage"
        payload = {'chat_id': chat_id, 'message_id': message_id}
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Error in delete_message_after_delay: {e}")

scheduler = BackgroundScheduler(daemon=True)
scheduler.start()

def escape_markdown(text: str) -> str:
    if not isinstance(text, str):
        return ''
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

# ======================================================================
# --- নোটিফিকেশন পাঠানোর ফাংশন (ওয়েবসাইট-কেন্দ্রিক) ---
# ======================================================================
def send_notification_to_channel(movie_data):
    """
    একটি নতুন কন্টেন্ট যোগ হলে বা ম্যানুয়ালি ট্রিগার করা হলে নোটিফিকেশন চ্যানেলে পোস্ট পাঠায়।
    এই ফাংশনটি ব্যবহারকারীকে শুধুমাত্র ওয়েবসাইটে পাঠায়।
    """
    if not NOTIFICATION_CHANNEL_ID:
        print("INFO: NOTIFICATION_CHANNEL_ID is not set. Skipping notification.")
        return

    try:
        # Flask application context এর মধ্যে URL তৈরি করতে হবে
        with app.app_context():
            movie_url = url_for('movie_detail', movie_id=str(movie_data['_id']), _external=True)

        title = movie_data.get('title', 'N/A')
        poster_url = movie_data.get('poster')
        is_coming_soon = movie_data.get('is_coming_soon', False)

        # পোস্টার না থাকলে বা প্লেসহোল্ডার হলে নোটিফিকেশন পাঠাবে না
        if not poster_url or not poster_url.startswith('http') or poster_url == PLACEHOLDER_POSTER:
            print(f"WARNING: Invalid or missing poster for '{title}'. Skipping photo notification.")
            return

        # "Coming Soon" কন্টেন্টের জন্য আলাদা ক্যাপশন
        if is_coming_soon:
            caption = (
                f"⏳ **Coming Soon!** ⏳\n\n"
                f"🎬 **{title}**\n\n"
                f"Get ready! This content will be available on our platform very soon. Stay tuned!"
            )
            keyboard = {} # কোনো বাটন থাকবে না
        else:
            # সাধারণ কন্টেন্টের জন্য বিস্তারিত ক্যাপশন
            year = movie_data.get('release_date', '----').split('-')[0]
            genres = ", ".join(movie_data.get('genres', []))
            rating = movie_data.get('vote_average', 0)
            
            caption = f"✨ **New Content Added!** ✨\n\n🎬 **{title} ({year})**\n"
            if genres:
                caption += f"🎭 **Genre:** {genres}\n"
            if rating > 0:
                caption += f"⭐ **Rating:** {rating:.1f}/10\n"
            
            caption += "\n👇 Click the button below to watch or download now from our website!"

            # শুধুমাত্র ওয়েবসাইটের জন্য বাটন
            keyboard = {
                "inline_keyboard": [
                    [{"text": "➡️ Watch / Download on Website", "url": movie_url}]
                ]
            }

        # Telegram API-এর জন্য পেলোড প্রস্তুত করা
        api_url = f"{TELEGRAM_API_URL}/sendPhoto"
        payload = {
            'chat_id': NOTIFICATION_CHANNEL_ID,
            'photo': poster_url,
            'caption': caption,
            'parse_mode': 'Markdown',
            'reply_markup': json.dumps(keyboard) if keyboard else None
        }

        # রিকোয়েস্ট পাঠানো
        response = requests.post(api_url, data=payload, timeout=15)
        response.raise_for_status()

        response_data = response.json()
        if response_data.get('ok'):
            print(f"SUCCESS: Notification sent for '{title}'.")
            
            # যদি কন্টেন্ট ট্রেন্ডিং হয় এবং Coming Soon না হয়, তাহলে মেসেজটি পিন করুন
            if movie_data.get('is_trending') and not is_coming_soon:
                message_id = response_data['result']['message_id']
                pin_url = f"{TELEGRAM_API_URL}/pinChatMessage"
                pin_payload = {'chat_id': NOTIFICATION_CHANNEL_ID, 'message_id': message_id}
                pin_response = requests.post(pin_url, json=pin_payload)
                if pin_response.json().get('ok'):
                    print(f"SUCCESS: Message {message_id} pinned in the channel.")
        else:
            print(f"ERROR: Failed to send notification. Telegram API response: {response.text}")

    except Exception as e:
        print(f"FATAL ERROR in send_notification_to_channel: {e}")

# ======================================================================
# --- HTML টেমপ্লেট ---
# ======================================================================

# [নতুন ডিজাইন] index_html টেমপ্লেট
index_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>EonMovies - Watch Movies & Series Online</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.2.0/css/all.min.css">
    <style>
        :root {
            --primary-color: #e50914;
            --background-color: #111;
            --card-color: #1a1a1a;
            --text-color: #fff;
            --text-muted-color: #aaa;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Poppins', sans-serif;
            background-color: var(--background-color);
            color: var(--text-color);
        }
        a { text-decoration: none; color: inherit; }
        .container { max-width: 1400px; margin: 0 auto; padding: 0 20px; }

        /* --- Header --- */
        .header {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 20px 40px;
            z-index: 100;
            transition: background-color 0.3s ease;
        }
        .header.scrolled { background-color: rgba(17, 17, 17, 0.9); backdrop-filter: blur(5px); }
        .logo { font-size: 2rem; font-weight: 700; color: var(--primary-color); }
        .search-bar { position: relative; }
        .search-input {
            background-color: rgba(0,0,0,0.5);
            border: 1px solid #333;
            color: var(--text-color);
            padding: 8px 15px;
            border-radius: 20px;
            width: 250px;
            transition: all 0.3s ease;
        }
        .search-input:focus { outline: none; border-color: var(--primary-color); }

        /* --- Hero Section --- */
        .hero-section {
            height: 80vh;
            position: relative;
            display: flex;
            align-items: center;
            color: white;
            background-size: cover;
            background-position: center top;
        }
        .hero-section::before {
            content: '';
            position: absolute;
            top: 0; left: 0; right: 0; bottom: 0;
            background: linear-gradient(to top, var(--background-color) 10%, transparent 50%), 
                        linear-gradient(to right, rgba(17,17,17,0.8) 0%, transparent 60%);
        }
        .hero-content { position: relative; z-index: 2; padding: 0 40px; max-width: 50%; }
        .hero-title { font-size: 3.5rem; font-weight: 700; margin-bottom: 1rem; line-height: 1.2; }
        .hero-overview { font-size: 1rem; color: var(--text-muted-color); line-height: 1.6; margin-bottom: 1.5rem; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
        .hero-button {
            padding: 10px 25px;
            background-color: var(--primary-color);
            color: white;
            border-radius: 5px;
            font-weight: 600;
            transition: opacity 0.3s ease;
        }
        .hero-button:hover { opacity: 0.8; }

        /* --- Main Content --- */
        main { padding-top: 2rem; }
        .content-section { margin-bottom: 40px; }
        .section-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }
        .section-title { font-size: 1.8rem; font-weight: 600; }
        .see-all-link { color: var(--text-muted-color); font-weight: 500; }
        
        /* --- Movie Grid & Card --- */
        .movie-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
            gap: 20px 15px;
        }
        .movie-card {
            background-color: var(--card-color);
            border-radius: 8px;
            overflow: hidden;
            transition: transform 0.3s ease, box-shadow 0.3s ease;
        }
        .movie-card:hover {
            transform: scale(1.05);
            box-shadow: 0 10px 20px rgba(0,0,0,0.5);
        }
        .movie-poster { width: 100%; height: auto; aspect-ratio: 2 / 3; object-fit: cover; display: block; }
        .movie-card-info { padding: 10px; }
        .movie-card-title {
            font-size: 0.95rem;
            font-weight: 500;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .movie-card-meta { font-size: 0.8rem; color: var(--text-muted-color); }
        
        .full-page-grid-container { padding: 120px 40px 50px; }
        .full-page-grid-title { font-size: 2.5rem; font-weight: 700; margin-bottom: 30px; }

        /* --- Footer --- */
        .footer {
            padding: 40px;
            margin-top: 50px;
            background-color: var(--card-color);
            text-align: center;
        }
        .footer-logo { font-size: 1.5rem; font-weight: 700; color: var(--primary-color); margin-bottom: 10px; }
        .footer-text { color: var(--text-muted-color); }
        .footer a { color: var(--primary-color); }

        @media (max-width: 768px) {
            .header { padding: 15px 20px; }
            .logo { font-size: 1.8rem; }
            .search-input { width: 150px; }
            .hero-section { height: 60vh; }
            .hero-content { max-width: 90%; padding: 0 20px; }
            .hero-title { font-size: 2.5rem; }
            .hero-overview { font-size: 0.9rem; -webkit-line-clamp: 2; }
            .section-title { font-size: 1.4rem; }
            .movie-grid { grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 15px 10px; }
            .full-page-grid-container { padding: 100px 20px 30px; }
        }
    </style>
</head>
<body>

    <header class="header">
        <a href="{{ url_for('home') }}" class="logo">EonMovies</a>
        <form method="GET" action="/" class="search-bar">
            <input type="search" name="q" class="search-input" placeholder="Search movies, series..." value="{{ query|default('') }}">
        </form>
    </header>

    {% macro render_movie_card(m) %}
        <a href="{{ url_for('movie_detail', movie_id=m._id) }}" class="movie-card">
            <img class="movie-poster" loading="lazy" src="{{ m.poster or 'https://via.placeholder.com/400x600.png?text=No+Image' }}" alt="{{ m.title }}">
            <div class="movie-card-info">
                <h4 class="movie-card-title">{{ m.title }}</h4>
                {% if m.release_date %}<p class="movie-card-meta">{{ m.release_date.split('-')[0] }}</p>{% endif %}
            </div>
        </a>
    {% endmacro %}

    <main class="container">
        {% if is_full_page_list %}
            <div class="full-page-grid-container">
                <h2 class="full-page-grid-title">{{ query }}</h2>
                {% if movies|length == 0 %}
                    <p style="text-align:center; color: var(--text-muted-color); margin-top: 40px;">No content found.</p>
                {% else %}
                    <div class="movie-grid">
                        {% for m in movies %}
                            {{ render_movie_card(m) }}
                        {% endfor %}
                    </div>
                {% endif %}
            </div>
        {% else %}
            <!-- Hero Section -->
            {% if recently_added %}
            <div class="hero-section" style="background-image: url('{{ (recently_added[0].poster or '').replace('w500', 'original') }}');">
                <div class="hero-content">
                    <h1 class="hero-title">{{ recently_added[0].title }}</h1>
                    <p class="hero-overview">{{ recently_added[0].overview }}</p>
                    <a href="{{ url_for('movie_detail', movie_id=recently_added[0]._id) }}" class="hero-button">
                        <i class="fas fa-info-circle"></i> View Details
                    </a>
                </div>
            </div>
            {% endif %}

            <!-- Sections -->
            {% if ad_settings.banner_ad_code %}<div style="margin: 40px 0; text-align: center;">{{ ad_settings.banner_ad_code|safe }}</div>{% endif %}
            
            {% if trending_movies %}
            <section class="content-section">
                <div class="section-header"><h2 class="section-title">Trending Now</h2><a href="{{ url_for('trending_movies') }}" class="see-all-link">See All</a></div>
                <div class="movie-grid">{% for m in trending_movies %}{{ render_movie_card(m) }}{% endfor %}</div>
            </section>
            {% endif %}

            {% if latest_movies %}
            <section class="content-section">
                <div class="section-header"><h2 class="section-title">Latest Movies</h2><a href="{{ url_for('movies_only') }}" class="see-all-link">See All</a></div>
                <div class="movie-grid">{% for m in latest_movies %}{{ render_movie_card(m) }}{% endfor %}</div>
            </section>
            {% endif %}
            
            {% if ad_settings.native_banner_code %}<div style="margin: 40px 0; text-align: center;">{{ ad_settings.native_banner_code|safe }}</div>{% endif %}

            {% if latest_series %}
            <section class="content-section">
                <div class="section-header"><h2 class="section-title">Latest Series</h2><a href="{{ url_for('webseries') }}" class="see-all-link">See All</a></div>
                <div class="movie-grid">{% for m in latest_series %}{{ render_movie_card(m) }}{% endfor %}</div>
            </section>
            {% endif %}
            
            {% if coming_soon_movies %}
            <section class="content-section">
                <div class="section-header"><h2 class="section-title">Coming Soon</h2><a href="{{ url_for('coming_soon') }}" class="see-all-link">See All</a></div>
                <div class="movie-grid">{% for m in coming_soon_movies %}{{ render_movie_card(m) }}{% endfor %}</div>
            </section>
            {% endif %}
        {% endif %}
    </main>
    
    <footer class="footer">
        <a href="{{ url_for('home') }}" class="footer-logo">EonMovies</a>
        <p class="footer-text">Watch the latest movies and web series for free. All rights reserved.</p>
        <p class="footer-text">Join our <a href="{{ main_channel_link or '#' }}" target="_blank">Telegram Channel</a> for updates!</p>
    </footer>

    <script>
        const header = document.querySelector('.header');
        window.addEventListener('scroll', () => {
            if (window.scrollY > 50) {
                header.classList.add('scrolled');
            } else {
                header.classList.remove('scrolled');
            }
        });
    </script>
    {% if ad_settings.popunder_code %}{{ ad_settings.popunder_code|safe }}{% endif %}
</body>
</html>
"""

# [নতুন ডিজাইন] detail_html টেমপ্লেট
detail_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ movie.title if movie else "Content Not Found" }} - EonMovies</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.2.0/css/all.min.css">
    <style>
        :root {
            --primary-color: #e50914;
            --background-color: #111;
            --card-color: #1a1a1a;
            --text-color: #fff;
            --text-muted-color: #aaa;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Poppins', sans-serif;
            background-color: var(--background-color);
            color: var(--text-color);
        }
        a { text-decoration: none; color: inherit; }
        .container { max-width: 1200px; margin: 0 auto; padding: 0 20px; }

        /* --- Header --- */
        .header { position: absolute; top: 0; left: 0; width: 100%; padding: 20px 40px; z-index: 10; }
        .back-button { font-weight: 600; font-size: 1rem; }
        .back-button:hover { color: var(--primary-color); }
        
        /* --- Movie Banner (Background) --- */
        .movie-banner {
            position: relative;
            padding: 150px 0 50px 0;
            background-size: cover;
            background-position: center;
        }
        .movie-banner::before {
            content: '';
            position: absolute;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(17, 17, 17, 0.8);
            backdrop-filter: blur(10px);
        }
        .movie-banner::after {
            content: '';
            position: absolute;
            bottom: 0; left: 0; right: 0;
            height: 100px;
            background: linear-gradient(to top, var(--background-color), transparent);
        }

        /* --- Movie Content --- */
        .movie-content {
            position: relative;
            z-index: 2;
            display: flex;
            gap: 40px;
        }
        .movie-poster {
            width: 300px;
            height: 450px;
            flex-shrink: 0;
            border-radius: 12px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.5);
            object-fit: cover;
        }
        .movie-info { flex-grow: 1; }
        .movie-title { font-size: 3rem; font-weight: 700; line-height: 1.2; margin-bottom: 1rem; }
        .movie-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 15px; margin-bottom: 1.5rem; color: var(--text-muted-color); }
        .meta-item { display: flex; align-items: center; gap: 5px; font-weight: 500; }
        .meta-item .fa-star { color: #f5c518; }
        .movie-genres { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 1.5rem; }
        .genre-tag { background-color: var(--card-color); padding: 5px 12px; border-radius: 20px; font-size: 0.8rem; }
        .movie-overview { line-height: 1.7; color: #ccc; margin-bottom: 2rem; }
        
        /* --- Buttons & Links --- */
        .action-buttons a {
            display: inline-flex;
            align-items: center;
            gap: 10px;
            padding: 12px 25px;
            border-radius: 5px;
            font-weight: 600;
            margin-right: 15px;
            margin-bottom: 15px;
            transition: transform 0.2s ease;
        }
        .action-buttons a:hover { transform: scale(1.05); }
        .btn-watch { background-color: var(--primary-color); color: #fff; }
        .btn-telegram { background-color: #2AABEE; color: #fff; }
        .btn-download { background-color: #333; color: #fff; }
        
        .section-title { font-size: 1.8rem; font-weight: 600; margin: 50px 0 20px 0; border-left: 4px solid var(--primary-color); padding-left: 10px; }
        
        /* --- Episodes --- */
        .episode-list { display: flex; flex-direction: column; gap: 10px; }
        .episode-item { display: flex; justify-content: space-between; align-items: center; padding: 15px; background-color: var(--card-color); border-radius: 5px; }
        .episode-title { font-weight: 500; }
        
        /* --- Related Movies --- */
        .related-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
            gap: 20px 15px;
        }
        .movie-card { background-color: var(--card-color); border-radius: 8px; overflow: hidden; transition: transform 0.3s ease; }
        .movie-card:hover { transform: scale(1.05); }
        .movie-card-poster { width: 100%; height: auto; aspect-ratio: 2 / 3; object-fit: cover; }
        .movie-card-info { padding: 10px; }
        .movie-card-title { font-size: 0.95rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

        @media (max-width: 768px) {
            .movie-banner { padding: 100px 0 30px 0; }
            .movie-content { flex-direction: column; align-items: center; text-align: center; }
            .movie-poster { width: 60%; max-width: 250px; height: auto; margin-bottom: 20px; }
            .movie-title { font-size: 2.2rem; }
            .movie-meta, .movie-genres { justify-content: center; }
            .action-buttons a { width: 100%; justify-content: center; margin-right: 0; }
            .episode-item { flex-direction: column; gap: 10px; align-items: stretch; text-align: center;}
            .episode-item .action-buttons a { width: auto; }
        }
    </style>
</head>
<body>

    {% if movie %}
    <div class="movie-banner" style="background-image: url('{{ (movie.poster or '').replace('w500', 'original') }}');">
        <header class="header">
            <a href="{{ url_for('home') }}" class="back-button"><i class="fas fa-arrow-left"></i> Back to Home</a>
        </header>
        <div class="container">
            <div class="movie-content">
                <img class="movie-poster" src="{{ movie.poster or 'https://via.placeholder.com/400x600.png?text=No+Image' }}" alt="{{ movie.title }}">
                <div class="movie-info">
                    <h1 class="movie-title">{{ movie.title }}</h1>
                    <div class="movie-meta">
                        {% if movie.release_date %}<span class="meta-item"><i class="fas fa-calendar-alt"></i> {{ movie.release_date.split('-')[0] }}</span>{% endif %}
                        {% if movie.vote_average %}<span class="meta-item"><i class="fas fa-star"></i> {{ "%.1f"|format(movie.vote_average) }}/10</span>{% endif %}
                        {% if movie.languages %}<span class="meta-item"><i class="fas fa-language"></i> {{ movie.languages | join(', ') }}</span>{% endif %}
                    </div>
                    {% if movie.genres %}
                    <div class="movie-genres">
                        {% for genre in movie.genres %}<span class="genre-tag">{{ genre }}</span>{% endfor %}
                    </div>
                    {% endif %}
                    <p class="movie-overview">{{ movie.overview }}</p>
                    
                    <div class="action-buttons">
                        {% if movie.type == 'movie' and movie.watch_link and not movie.is_coming_soon %}
                            <a href="{{ url_for('watch_movie', movie_id=movie._id) }}" class="btn-watch"><i class="fas fa-play"></i> Watch Now</a>
                        {% endif %}
                    </div>
                </div>
            </div>
        </div>
    </div>

    <div class="container main-details">
        {% if ad_settings.banner_ad_code %}<div style="margin: 40px 0; text-align: center;">{{ ad_settings.banner_ad_code|safe }}</div>{% endif %}

        <!-- Download & Episode Section -->
        {% if movie.is_coming_soon %}
            <h2 class="section-title">Coming Soon...</h2>
            <p>This content will be available soon. Stay tuned!</p>
        {% elif movie.type == 'movie' %}
            <h2 class="section-title">Download Links</h2>
            <div class="action-buttons">
                {% for link_item in movie.links %}
                    <a href="{{ link_item.url }}" target="_blank" rel="noopener" class="btn-download"><i class="fas fa-download"></i> Download {{ link_item.quality }}</a>
                {% endfor %}
                {% for file in movie.files | sort(attribute='quality') %}
                    <a href="https://t.me/{{ bot_username }}?start={{ movie._id }}_{{ file.quality }}" class="btn-telegram"><i class="fab fa-telegram"></i> Get {{ file.quality }}</a>
                {% endfor %}
            </div>
        {% elif movie.type == 'series' %}
            {% if movie.season_packs %}
                <h2 class="section-title">Season Packs</h2>
                <div class="episode-list">
                {% for pack in movie.season_packs | sort(attribute='quality') | sort(attribute='season') %}
                    <div class="episode-item">
                        <span class="episode-title">Complete Season {{ pack.season }} Pack ({{ pack.quality }})</span>
                        <div class="action-buttons">
                            <a href="https://t.me/{{ bot_username }}?start={{ movie._id }}_S{{ pack.season }}_{{ pack.quality }}" class="btn-telegram"><i class="fas fa-box-open"></i> Get Pack</a>
                        </div>
                    </div>
                {% endfor %}
                </div>
            {% endif %}
            
            {% if movie.episodes %}
                <h2 class="section-title">Episodes</h2>
                <div class="episode-list">
                {% for ep in movie.episodes | sort(attribute='episode_number') | sort(attribute='season') %}
                    <div class="episode-item">
                        <span class="episode-title">Season {{ ep.season }} - Episode {{ ep.episode_number }}</span>
                        <div class="action-buttons">
                           <a href="https://t.me/{{ bot_username }}?start={{ movie._id }}_{{ ep.season }}_{{ ep.episode_number }}" class="btn-telegram"><i class="fab fa-telegram"></i> Get Episode</a>
                        </div>
                    </div>
                {% endfor %}
                </div>
            {% endif %}
        {% endif %}
        
        <!-- Related Movies -->
        {% if related_movies %}
            <h2 class="section-title">You Might Also Like</h2>
            <div class="related-grid">
                {% for m in related_movies %}
                <a href="{{ url_for('movie_detail', movie_id=m._id) }}" class="movie-card">
                    <img class="movie-card-poster" loading="lazy" src="{{ m.poster or 'https://via.placeholder.com/400x600.png?text=No+Image' }}" alt="{{ m.title }}">
                    <div class="movie-card-info">
                        <h4 class="movie-card-title">{{ m.title }}</h4>
                    </div>
                </a>
                {% endfor %}
            </div>
        {% endif %}
    </div>

    {% else %}
        <div style="display:flex; justify-content:center; align-items:center; height:100vh;"><h2>Content not found.</h2></div>
    {% endif %}

    {% if ad_settings.popunder_code %}{{ ad_settings.popunder_code|safe }}{% endif %}
</body>
</html>
"""

genres_html = """
<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8" /><meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no" /><title>{{ title }} - MovieZone</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Roboto:wght@400;500;700&display=swap');
  :root { --netflix-red: #E50914; --netflix-black: #141414; --text-light: #f5f5f5; }
  * { box-sizing: border-box; margin: 0; padding: 0; } body { font-family: 'Roboto', sans-serif; background-color: var(--netflix-black); color: var(--text-light); } a { text-decoration: none; color: inherit; }
  .main-container { padding: 100px 50px 50px; } .page-title { font-family: 'Bebas Neue', sans-serif; font-size: 3rem; color: var(--netflix-red); margin-bottom: 30px; }
  .back-button { color: var(--text-light); font-size: 1rem; margin-bottom: 20px; display: inline-block; } .back-button:hover { color: var(--netflix-red); }
  .genre-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 20px; }
  .genre-card { background: linear-gradient(45deg, #2c2c2c, #1a1a1a); border-radius: 8px; padding: 30px 20px; text-align: center; font-size: 1.4rem; font-weight: 700; transition: all 0.3s ease; border: 1px solid #444; }
  .genre-card:hover { transform: translateY(-5px) scale(1.03); background: linear-gradient(45deg, var(--netflix-red), #b00710); border-color: var(--netflix-red); }
  @media (max-width: 768px) { .main-container { padding: 80px 15px 30px; } .page-title { font-size: 2.2rem; } .genre-grid { grid-template-columns: repeat(2, 1fr); gap: 15px; } .genre-card { font-size: 1.1rem; padding: 25px 15px; } }
</style><link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.2.0/css/all.min.css"></head>
<body>
<div class="main-container"><a href="{{ url_for('home') }}" class="back-button"><i class="fas fa-arrow-left"></i> Back to Home</a><h1 class="page-title">{{ title }}</h1>
<div class="genre-grid">{% for genre in genres %}<a href="{{ url_for('movies_by_genre', genre_name=genre) }}" class="genre-card"><span>{{ genre }}</span></a>{% endfor %}</div></div>
{% if ad_settings.popunder_code %}{{ ad_settings.popunder_code|safe }}{% endif %}
{% if ad_settings.social_bar_code %}{{ ad_settings.social_bar_code|safe }}{% endif %}
</body></html>
"""
watch_html = """
<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Watching: {{ title }}</title>
<style> body, html { margin: 0; padding: 0; height: 100%; overflow: hidden; background-color: #000; } .player-container { width: 100%; height: 100%; } .player-container iframe { width: 100%; height: 100%; border: 0; } </style></head>
<body><div class="player-container"><iframe src="{{ watch_link }}" allowfullscreen allowtransparency allow="autoplay" scrolling="no" frameborder="0"></iframe></div>
{% if ad_settings.popunder_code %}{{ ad_settings.popunder_code|safe }}{% endif %}
{% if ad_settings.social_bar_code %}{{ ad_settings.social_bar_code|safe }}{% endif %}
</body></html>
"""
admin_html = """
<!DOCTYPE html>
<html><head><title>Admin Panel - MovieZone</title><meta name="viewport" content="width=device-width, initial-scale=1" /><style>
:root { --netflix-red: #E50914; --netflix-black: #141414; --dark-gray: #222; --light-gray: #333; --text-light: #f5f5f5; }
body { font-family: 'Roboto', sans-serif; background: var(--netflix-black); color: var(--text-light); padding: 20px; }
h2, h3 { font-family: 'Bebas Neue', sans-serif; color: var(--netflix-red); } h2 { font-size: 2.5rem; margin-bottom: 20px; } h3 { font-size: 1.5rem; margin: 20px 0 10px 0;}
form { max-width: 800px; margin: 0 auto 40px auto; background: var(--dark-gray); padding: 25px; border-radius: 8px;}
.form-group { margin-bottom: 15px; } .form-group label { display: block; margin-bottom: 8px; font-weight: bold; }
input[type="text"], input[type="url"], input[type="search"], textarea, select, input[type="number"], input[type="email"] { width: 100%; padding: 12px; border-radius: 4px; border: 1px solid var(--light-gray); font-size: 1rem; background: var(--light-gray); color: var(--text-light); box-sizing: border-box; }
input[type="checkbox"] { width: auto; margin-right: 10px; transform: scale(1.2); } textarea { resize: vertical; min-height: 100px; }
button[type="submit"], .add-btn, .clear-btn { background: var(--netflix-red); color: white; font-weight: 700; cursor: pointer; border: none; padding: 12px 25px; border-radius: 4px; font-size: 1rem; transition: background 0.3s ease; text-decoration: none; }
button[type="submit"]:hover, .add-btn:hover { background: #b00710; }
.clear-btn { background: #555; display: inline-block; } .clear-btn:hover { background: #444; }
table { display: block; overflow-x: auto; white-space: nowrap; width: 100%; border-collapse: collapse; margin-top: 20px; }
th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid var(--light-gray); } th { background: #252525; } td { background: var(--dark-gray); }
.action-buttons { display: flex; gap: 10px; } .action-buttons a, .action-buttons button, .delete-btn { padding: 6px 12px; border-radius: 4px; text-decoration: none; color: white; border: none; cursor: pointer; }
.edit-btn { background: #007bff; } .delete-btn { background: #dc3545; } .notify-btn { background: #17a2b8; }
.dynamic-item { border: 1px solid var(--light-gray); padding: 15px; margin-bottom: 15px; border-radius: 5px; }
hr.section-divider { border: 0; height: 2px; background-color: var(--light-gray); margin: 40px 0; }
.danger-zone { border: 2px solid var(--netflix-red); padding: 20px; border-radius: 8px; margin-top: 20px; text-align: center; }
.danger-zone-btn { background: #dc3545; color: white; text-decoration: none; padding: 10px 20px; border-radius: 5px; font-weight: bold; }
</style><link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Roboto:wght@400;700&display=swap" rel="stylesheet"></head>
<body>
  <h2>বিজ্ঞাপন পরিচালনা (Ad Management)</h2>
  <form action="{{ url_for('save_ads') }}" method="post"><div class="form-group"><label>Pop-Under / OnClick Ad Code</label><textarea name="popunder_code" rows="4">{{ ad_settings.popunder_code or '' }}</textarea></div><div class="form-group"><label>Social Bar / Sticky Ad Code</label><textarea name="social_bar_code" rows="4">{{ ad_settings.social_bar_code or '' }}</textarea></div><div class="form-group"><label>ব্যানার বিজ্ঞাপন কোড (Banner Ad)</label><textarea name="banner_ad_code" rows="4">{{ ad_settings.banner_ad_code or '' }}</textarea></div><div class="form-group"><label>নেটিভ ব্যানার বিজ্ঞাপন (Native Banner)</label><textarea name="native_banner_code" rows="4">{{ ad_settings.native_banner_code or '' }}</textarea></div><button type="submit">Save Ad Codes</button></form>
  <hr class="section-divider">
  <h2>Add New Content (Manual)</h2>
  <form method="post" action="{{ url_for('admin') }}">
    <div class="form-group"><label>Title (Required):</label><input type="text" name="title" required /></div>
    <div class="form-group"><label>Content Type:</label><select name="content_type" id="content_type" onchange="toggleFields()"><option value="movie">Movie</option><option value="series">TV/Web Series</option></select></div>
    <div id="movie_fields">
      <div class="form-group"><label>Watch Link (Embed URL):</label><input type="url" name="watch_link" /></div><hr><p><b>OR</b> Download Links (Manual)</p>
      <div class="form-group"><label>480p Link:</label><input type="url" name="link_480p" /></div>
      <div class="form-group"><label>720p Link:</label><input type="url" name="link_720p" /></div>
      <div class="form-group"><label>1080p Link:</label><input type="url" name="link_1080p" /></div>
      <hr><p><b>OR</b> Get from Telegram</p>
      <div id="telegram_files_container"></div><button type="button" onclick="addTelegramFileField()" class="add-btn">Add Telegram File</button>
    </div>
    <div id="episode_fields" style="display: none;">
      <h3>Episodes</h3><div id="episodes_container"></div>
      <button type="button" onclick="addEpisodeField()" class="add-btn">Add Episode</button>
    </div>
    <hr style="margin: 20px 0;"><button type="submit">Add Content</button>
  </form>
  <hr class="section-divider">
  <h2>Manage Content</h2>
  <form method="GET" action="{{ url_for('admin') }}" style="padding: 15px; background: #252525; display: flex; gap: 10px; align-items: center;">
    <input type="search" name="search" placeholder="Search by title..." value="{{ search_query or '' }}" style="flex-grow: 1;">
    <button type="submit">Search</button>
    {% if search_query %}<a href="{{ url_for('admin') }}" class="clear-btn">Clear</a>{% endif %}
  </form>
  <table><thead><tr><th>Title</th><th>Type</th><th>Actions</th></tr></thead><tbody>
    {% for movie in content_list %}
    <tr><td>{{ movie.title }}</td><td>{{ movie.type | title }}</td>
        <td class="action-buttons">
            <a href="{{ url_for('edit_movie', movie_id=movie._id) }}" class="edit-btn">Edit</a>
            <a href="{{ url_for('send_manual_notification', movie_id=movie._id) }}" class="notify-btn" onclick="return confirm('Are you sure you want to send a notification for \\'{{ movie.title }}\\' to the channel?')">Notify</a>
            <button class="delete-btn" onclick="confirmDelete('{{ movie._id }}', '{{ movie.title }}')">Delete</button>
        </td>
    </tr>
    {% else %}
    <tr><td colspan="3" style="text-align: center;">No content found.</td></tr>
    {% endfor %}
  </tbody></table>
  
  <div class="danger-zone">
      <h3>DANGER ZONE</h3>
      <p style="margin-bottom: 15px;">This will permanently delete all movies and series from the database. This action cannot be undone.</p>
      <a href="{{ url_for('delete_all_movies') }}" class="danger-zone-btn" onclick="return confirm('ARE YOU ABSOLUTELY SURE?\\nThis will delete ALL content from the database permanently.\\nThis action cannot be undone.');">Delete All Content</a>
  </div>

  <hr class="section-divider">
  <h2>User Feedback / Reports</h2>
  {% if feedback_list %}<table><thead><tr><th>Date</th><th>Type</th><th>Title</th><th>Message</th><th>Email</th><th>Action</th></tr></thead><tbody>{% for item in feedback_list %}<tr><td style="min-width: 150px;">{{ item.timestamp.strftime('%Y-%m-%d %H:%M') }}</td><td>{{ item.type }}</td><td>{{ item.content_title }}</td><td style="white-space: pre-wrap; min-width: 300px;">{{ item.message }}</td><td>{{ item.email or 'N/A' }}</td><td><a href="{{ url_for('delete_feedback', feedback_id=item._id) }}" class="delete-btn" onclick="return confirm('Delete this feedback?');">Delete</a></td></tr>{% endfor %}</tbody></table>{% else %}<p>No new feedback or reports.</p>{% endif %}
  <script>
    function confirmDelete(id, title) { if (confirm('Delete "' + title + '"?')) window.location.href = '/delete_movie/' + id; }
    function toggleFields() { var isSeries = document.getElementById('content_type').value === 'series'; document.getElementById('episode_fields').style.display = isSeries ? 'block' : 'none'; document.getElementById('movie_fields').style.display = isSeries ? 'none' : 'block'; }
    function addTelegramFileField() { const c = document.getElementById('telegram_files_container'); const d = document.createElement('div'); d.className = 'dynamic-item'; d.innerHTML = `<div class="form-group"><label>Quality (e.g., 720p):</label><input type="text" name="telegram_quality[]" required /></div><div class="form-group"><label>Message ID:</label><input type="number" name="telegram_message_id[]" required /></div><button type="button" onclick="this.parentElement.remove()" class="delete-btn">Remove</button>`; c.appendChild(d); }
    function addEpisodeField() { const c = document.getElementById('episodes_container'); const d = document.createElement('div'); d.className = 'dynamic-item'; d.innerHTML = `<div class="form-group"><label>Season Number:</label><input type="number" name="episode_season[]" value="1" required /></div><div class="form-group"><label>Episode Number:</label><input type="number" name="episode_number[]" required /></div><div class="form-group"><label>Episode Title:</label><input type="text" name="episode_title[]" /></div><hr><p><b>Provide ONE of the following:</b></p><div class="form-group"><label>Telegram Message ID:</label><input type="number" name="episode_message_id[]" /></div><p><b>OR</b> Watch Link:</p><div class="form-group"><label>Watch Link (Embed):</label><input type="url" name="episode_watch_link[]" /></div><button type="button" onclick="this.parentElement.remove()" class="delete-btn">Remove Episode</button>`; c.appendChild(d); }
    document.addEventListener('DOMContentLoaded', toggleFields);
  </script>
</body></html>
"""
edit_html = """
<!DOCTYPE html>
<html><head><title>Edit Content - MovieZone</title><meta name="viewport" content="width=device-width, initial-scale=1" /><style>
:root { --netflix-red: #E50914; --netflix-black: #141414; --dark-gray: #222; --light-gray: #333; --text-light: #f5f5f5; }
body { font-family: 'Roboto', sans-serif; background: var(--netflix-black); color: var(--text-light); padding: 20px; }
h2, h3 { font-family: 'Bebas Neue', sans-serif; color: var(--netflix-red); } h2 { font-size: 2.5rem; margin-bottom: 20px; } h3 { font-size: 1.5rem; margin: 20px 0 10px 0;}
form { max-width: 800px; margin: 0 auto 40px auto; background: var(--dark-gray); padding: 25px; border-radius: 8px;}
.form-group { margin-bottom: 15px; } .form-group label { display: block; margin-bottom: 8px; font-weight: bold; }
input, textarea, select { width: 100%; padding: 12px; border-radius: 4px; border: 1px solid var(--light-gray); font-size: 1rem; background: var(--light-gray); color: var(--text-light); box-sizing: border-box; }
input[type="checkbox"] { width: auto; margin-right: 10px; transform: scale(1.2); } textarea { resize: vertical; min-height: 100px; }
button[type="submit"], .add-btn { background: var(--netflix-red); color: white; font-weight: 700; cursor: pointer; border: none; padding: 12px 25px; border-radius: 4px; font-size: 1rem; }
.back-to-admin { display: inline-block; margin-bottom: 20px; color: var(--netflix-red); text-decoration: none; font-weight: bold; }
.dynamic-item { border: 1px solid var(--light-gray); padding: 15px; margin-bottom: 15px; border-radius: 5px; } .delete-btn { background: #dc3545; color: white; border: none; padding: 6px 12px; border-radius: 4px; cursor: pointer; }
</style><link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Roboto:wght@400;700&display=swap" rel="stylesheet"></head>
<body>
  <a href="{{ url_for('admin') }}" class="back-to-admin">← Back to Admin</a>
  <h2>Edit: {{ movie.title }}</h2>
  <form method="post">
    <div class="form-group"><label>Title:</label><input type="text" name="title" value="{{ movie.title }}" required /></div>
    <div class="form-group"><label>Poster URL:</label><input type="url" name="poster" value="{{ movie.poster or '' }}" /></div><div class="form-group"><label>Overview:</label><textarea name="overview">{{ movie.overview or '' }}</textarea></div>
    <div class="form-group"><label>Genres (comma separated):</label><input type="text" name="genres" value="{{ movie.genres|join(', ') if movie.genres else '' }}" /></div>
    <div class="form-group"><label>Languages (comma separated):</label><input type="text" name="languages" value="{{ movie.languages|join(', ') if movie.languages else '' }}" placeholder="e.g. Hindi, English, Bangla" /></div>
    <div class="form-group"><label>Poster Badge:</label><input type="text" name="poster_badge" value="{{ movie.poster_badge or '' }}" /></div>
    <div class="form-group"><label>Content Type:</label><select name="content_type" id="content_type" onchange="toggleFields()"><option value="movie" {% if movie.type == 'movie' %}selected{% endif %}>Movie</option><option value="series" {% if movie.type == 'series' %}selected{% endif %}>TV/Web Series</option></select></div>
    
    <div id="movie_fields">
        <div class="form-group"><label>Watch Link:</label><input type="url" name="watch_link" value="{{ movie.watch_link or '' }}" /></div><hr><p><b>OR</b> Download Links (Manual)</p>
        <div class="form-group"><label>480p Link:</label><input type="url" name="link_480p" value="{% for l in movie.links %}{% if l.quality == '480p' %}{{ l.url }}{% endif %}{% endfor %}" /></div>
        <div class="form-group"><label>720p Link:</label><input type="url" name="link_720p" value="{% for l in movie.links %}{% if l.quality == '720p' %}{{ l.url }}{% endif %}{% endfor %}" /></div>
        <div class="form-group"><label>1080p Link:</label><input type="url" name="link_1080p" value="{% for l in movie.links %}{% if l.quality == '1080p' %}{{ l.url }}{% endif %}{% endfor %}" /></div>
        <hr><p><b>OR</b> Get from Telegram</p>
        <div id="telegram_files_container">
            {% if movie.type == 'movie' and movie.files %}{% for file in movie.files %}
            <div class="dynamic-item">
                <div class="form-group"><label>Quality:</label><input type="text" name="telegram_quality[]" value="{{ file.quality }}" required /></div>
                <div class="form-group"><label>Message ID:</label><input type="number" name="telegram_message_id[]" value="{{ file.message_id }}" required /></div>
                <button type="button" onclick="this.parentElement.remove()" class="delete-btn">Remove</button>
            </div>
            {% endfor %}{% endif %}
        </div><button type="button" onclick="addTelegramFileField()" class="add-btn">Add Telegram File</button>
    </div>

    <div id="episode_fields" style="display: none;">
      <h3>Season Packs</h3>
      <div id="season_packs_container">
        <!-- [START] MODIFIED SECTION: Edit form for Season Packs with Quality -->
        {% if movie.type == 'series' and movie.season_packs %}
          {% for pack in movie.season_packs | sort(attribute='season') %}
          <div class="dynamic-item">
            <div class="form-group"><label>Season Number:</label><input type="number" name="pack_season[]" value="{{ pack.season }}" required /></div>
            <div class="form-group"><label>Quality (e.g., 720p):</label><input type="text" name="pack_quality[]" value="{{ pack.quality }}" required /></div>
            <div class="form-group"><label>Telegram Message ID:</label><input type="number" name="pack_message_id[]" value="{{ pack.message_id }}" required /></div>
            <button type="button" onclick="this.parentElement.remove()" class="delete-btn">Remove Pack</button>
          </div>
          {% endfor %}
        {% endif %}
        <!-- [END] MODIFIED SECTION -->
      </div>
      <button type="button" onclick="addSeasonPackField()" class="add-btn">Add Season Pack</button>
      <hr style="margin: 20px 0;">

      <h3>Individual Episodes</h3>
      <div id="episodes_container">
      {% if movie.type == 'series' and movie.episodes %}{% for ep in movie.episodes | sort(attribute='episode_number') | sort(attribute='season') %}<div class="dynamic-item">
        <div class="form-group"><label>Season Number:</label><input type="number" name="episode_season[]" value="{{ ep.season or 1 }}" required /></div>
        <div class="form-group"><label>Ep Number:</label><input type="number" name="episode_number[]" value="{{ ep.episode_number }}" required /></div>
        <div class="form-group"><label>Ep Title:</label><input type="text" name="episode_title[]" value="{{ ep.title or '' }}" /></div>
        <hr><p><b>Provide ONE of the following:</b></p>
        <div class="form-group"><label>Telegram Message ID:</label><input type="number" name="episode_message_id[]" value="{{ ep.message_id or '' }}" /></div>
        <p><b>OR</b> Watch Link:</p>
        <div class="form-group"><label>Watch Link (Embed):</label><input type="url" name="episode_watch_link[]" value="{{ ep.watch_link or '' }}" /></div>
        <button type="button" onclick="this.parentElement.remove()" class="delete-btn">Remove Episode</button>
      </div>{% endfor %}{% endif %}</div><button type="button" onclick="addEpisodeField()" class="add-btn">Add Episode</button>
    </div>
    
    <hr style="margin: 20px 0;">
    <div class="form-group"><input type="checkbox" name="is_trending" value="true" {% if movie.is_trending %}checked{% endif %}><label style="display: inline-block;">Is Trending?</label></div>
    <div class="form-group"><input type="checkbox" name="is_coming_soon" value="true" {% if movie.is_coming_soon %}checked{% endif %}><label style="display: inline-block;">Is Coming Soon?</label></div>
    <button type="submit">Update Content</button>
  </form>
  
  <script>
    function toggleFields() { var isSeries = document.getElementById('content_type').value === 'series'; document.getElementById('episode_fields').style.display = isSeries ? 'block' : 'none'; document.getElementById('movie_fields').style.display = isSeries ? 'none' : 'block'; }
    function addTelegramFileField() { const c = document.getElementById('telegram_files_container'); const d = document.createElement('div'); d.className = 'dynamic-item'; d.innerHTML = `<div class="form-group"><label>Quality (e.g., 720p):</label><input type="text" name="telegram_quality[]" required /></div><div class="form-group"><label>Message ID:</label><input type="number" name="telegram_message_id[]" required /></div><button type="button" onclick="this.parentElement.remove()" class="delete-btn">Remove</button>`; c.appendChild(d); }
    function addEpisodeField() { const c = document.getElementById('episodes_container'); const d = document.createElement('div'); d.className = 'dynamic-item'; d.innerHTML = `<div class="form-group"><label>Season Number:</label><input type="number" name="episode_season[]" value="1" required /></div><div class="form-group"><label>Episode Number:</label><input type="number" name="episode_number[]" required /></div><div class="form-group"><label>Episode Title:</label><input type="text" name="episode_title[]" /></div><hr><p><b>Provide ONE of the following:</b></p><div class="form-group"><label>Telegram Message ID:</label><input type="number" name="episode_message_id[]" /></div><p><b>OR</b> Watch Link:</p><div class="form-group"><label>Watch Link (Embed):</label><input type="url" name="episode_watch_link[]" /></div><button type="button" onclick="this.parentElement.remove()" class="delete-btn">Remove Episode</button>`; c.appendChild(d); }
    
    // [START] MODIFIED SECTION: JavaScript function to add pack fields with quality
    function addSeasonPackField() { 
        const c = document.getElementById('season_packs_container'); 
        const d = document.createElement('div'); 
        d.className = 'dynamic-item'; 
        d.innerHTML = `<div class="form-group"><label>Season Number:</label><input type="number" name="pack_season[]" required /></div><div class="form-group"><label>Quality (e.g., 720p):</label><input type="text" name="pack_quality[]" required /></div><div class="form-group"><label>Telegram Message ID:</label><input type="number" name="pack_message_id[]" required /></div><button type="button" onclick="this.parentElement.remove()" class="delete-btn">Remove Pack</button>`; 
        c.appendChild(d); 
    }
    // [END] MODIFIED SECTION

    document.addEventListener('DOMContentLoaded', toggleFields);
  </script>
</body></html>
"""
contact_html = """
<!DOCTYPE html>
<html lang="bn"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Contact Us / Report - MovieZone</title><style>
:root { --netflix-red: #E50914; --netflix-black: #141414; --dark-gray: #222; --light-gray: #333; --text-light: #f5f5f5; }
body { font-family: 'Roboto', sans-serif; background: var(--netflix-black); color: var(--text-light); padding: 20px; display: flex; justify-content: center; align-items: center; min-height: 100vh; }
.contact-container { max-width: 600px; width: 100%; background: var(--dark-gray); padding: 30px; border-radius: 8px; }
h2 { font-family: 'Bebas Neue', sans-serif; color: var(--netflix-red); font-size: 2.5rem; text-align: center; margin-bottom: 25px; }
.form-group { margin-bottom: 20px; } label { display: block; margin-bottom: 8px; font-weight: bold; }
input, select, textarea { width: 100%; padding: 12px; border-radius: 4px; border: 1px solid var(--light-gray); font-size: 1rem; background: var(--light-gray); color: var(--text-light); box-sizing: border-box; }
textarea { resize: vertical; min-height: 120px; } button[type="submit"] { background: var(--netflix-red); color: white; font-weight: 700; cursor: pointer; border: none; padding: 12px 25px; border-radius: 4px; font-size: 1.1rem; width: 100%; }
.success-message { text-align: center; padding: 20px; background-color: #1f4e2c; color: #d4edda; border-radius: 5px; margin-bottom: 20px; }
.back-link { display: block; text-align: center; margin-top: 20px; color: var(--netflix-red); text-decoration: none; font-weight: bold; }
</style><link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Roboto:wght@400;700&display=swap" rel="stylesheet"></head>
<body><div class="contact-container"><h2>Contact Us</h2>
{% if message_sent %}<div class="success-message"><p>আপনার বার্তা সফলভাবে পাঠানো হয়েছে। ধন্যবাদ!</p></div><a href="{{ url_for('home') }}" class="back-link">← Back to Home</a>
{% else %}<form method="post"><div class="form-group"><label for="type">বিষয় (Subject):</label><select name="type" id="type"><option value="Movie Request" {% if prefill_type == 'Problem Report' %}disabled{% endif %}>Movie/Series Request</option><option value="Problem Report" {% if prefill_type == 'Problem Report' %}selected{% endif %}>Report a Problem</option><option value="General Feedback">General Feedback</option></select></div><div class="form-group"><label for="content_title">মুভি/সিরিজের নাম (Title):</label><input type="text" name="content_title" id="content_title" value="{{ prefill_title }}" required></div><div class="form-group"><label for="message">আপনার বার্তা (Message):</label><textarea name="message" id="message" required></textarea></div><div class="form-group"><label for="email">আপনার ইমেইল (Optional):</label><input type="email" name="email" id="email"></div><input type="hidden" name="reported_content_id" value="{{ prefill_id }}"><button type="submit">Submit</button></form><a href="{{ url_for('home') }}" class="back-link">← Cancel</a>{% endif %}
</div></body></html>
"""

# ======================================================================
# --- Helper Functions ---
# ======================================================================

def parse_filename(filename):
    LANGUAGE_MAP = {
        'hindi': 'Hindi', 'hin': 'Hindi', 'english': 'English', 'eng': 'English',
        'bengali': 'Bengali', 'bangla': 'Bangla', 'ben': 'Bengali',
        'tamil': 'Tamil', 'tam': 'Tamil', 'telugu': 'Telugu', 'tel': 'Telugu',
        'kannada': 'Kannada', 'kan': 'Kannada', 'malayalam': 'Malayalam', 'mal': 'Malayalam',
        'korean': 'Korean', 'kor': 'Korean', 'chinese': 'Chinese', 'chi': 'Chinese',
        'japanese': 'Japanese', 'jap': 'Japanese',
        'dual audio': ['Hindi', 'English'], 'dual': ['Hindi', 'English'],
        'multi audio': ['Multi Audio']
    }
    JUNK_KEYWORDS = [
        '1080p', '720p', '480p', '2160p', '4k', 'uhd', 'web-dl', 'webdl', 'webrip',
        'brrip', 'bluray', 'dvdrip', 'hdrip', 'hdcam', 'camrip', 'hdts', 'x264',
        'x265', 'hevc', 'avc', 'aac', 'ac3', 'dts', '5.1', '7.1', 'final', 'uncut',
        'extended', 'remastered', 'unrated', 'nf', 'www', 'com', 'net', 'org', 'psa'
    ]
    SEASON_PACK_KEYWORDS = ['complete', 'season', 'pack', 'all episodes', 'zip']
    base_name, _ = os.path.splitext(filename)
    processed_name = re.sub(r'[\._\[\]\(\)\{\}-]', ' ', base_name)
    found_languages = []
    temp_name_for_lang = processed_name.lower()
    for keyword, lang_name in LANGUAGE_MAP.items():
        if re.search(r'\b' + re.escape(keyword) + r'\b', temp_name_for_lang):
            if isinstance(lang_name, list):
                found_languages.extend(lang_name)
            else:
                found_languages.append(lang_name)
    languages = sorted(list(set(found_languages))) if found_languages else []
    quality_match = re.search(r'\b(\d{3,4}p)\b', processed_name, re.I)
    quality = quality_match.group(1) if quality_match else "HD"
    season_pack_match = re.search(r'^(.*?)[\s\.]*(?:S|Season)[\s\.]?(\d{1,2})', processed_name, re.I)
    if season_pack_match:
        text_after_season = processed_name[season_pack_match.end():].lower()
        is_pack = any(keyword in text_after_season for keyword in SEASON_PACK_KEYWORDS) or not re.search(r'\be\d', text_after_season)
        if is_pack:
            title = season_pack_match.group(1).strip()
            season_num = int(season_pack_match.group(2))
            for junk in JUNK_KEYWORDS + SEASON_PACK_KEYWORDS:
                title = re.sub(r'\b' + re.escape(junk) + r'\b', '', title, flags=re.I)
            final_title = ' '.join(title.split()).title()
            if final_title:
                return {'type': 'series_pack', 'title': final_title, 'season': season_num, 'quality': quality, 'languages': languages}
    series_patterns = [
        re.compile(r'^(.*?)[\s\.]*(?:S|Season)[\s\.]?(\d{1,2})[\s\.]*(?:E|Ep|Episode)[\s\.]?(\d{1,3})', re.I),
        re.compile(r'^(.*?)[\s\.]*(?:E|Ep|Episode)[\s\.]?(\d{1,3})', re.I)
    ]
    for i, pattern in enumerate(series_patterns):
        match = pattern.search(processed_name)
        if match:
            title = match.group(1).strip()
            season_num = int(match.group(2)) if i == 0 else 1
            episode_num = int(match.group(3)) if i == 0 else int(match.group(2))
            for junk in JUNK_KEYWORDS:
                title = re.sub(r'\b' + re.escape(junk) + r'\b', '', title, flags=re.I)
            final_title = ' '.join(title.split()).title()
            if final_title:
                return {'type': 'series', 'title': final_title, 'season': season_num, 'episode': episode_num, 'languages': languages}
    year_match = re.search(r'\b(19[5-9]\d|20\d{2})\b', processed_name)
    year = year_match.group(1) if year_match else None
    title_part = processed_name[:year_match.start()] if year_match else processed_name
    temp_title = title_part
    for lang_key in LANGUAGE_MAP.keys():
        temp_title = re.sub(r'\b' + lang_key + r'\b', '', temp_title, flags=re.I)
    for junk in JUNK_KEYWORDS:
        temp_title = re.sub(r'\b' + re.escape(junk) + r'\b', '', temp_title, flags=re.I)
    final_title = ' '.join(temp_title.split()).title()
    return {'type': 'movie', 'title': final_title, 'year': year, 'quality': quality, 'languages': languages} if final_title else None

def get_tmdb_details_from_api(title, content_type, year=None):
    if not TMDB_API_KEY:
        print("ERROR: TMDB_API_KEY is not set.")
        return None
    search_type = "tv" if content_type in ["series", "series_pack"] else "movie"
    def search_tmdb(query_title):
        print(f"INFO: Searching TMDb for: '{query_title}' (Type: {search_type}, Year: {year})")
        try:
            search_url = f"https://api.themoviedb.org/3/search/{search_type}?api_key={TMDB_API_KEY}&query={requests.utils.quote(query_title)}"
            if year and search_type == "movie":
                search_url += f"&primary_release_year={year}"
            search_res = requests.get(search_url, timeout=10)
            search_res.raise_for_status()
            results = search_res.json().get("results")
            if not results: return None
            tmdb_id = results[0].get("id")
            detail_url = f"https://api.themoviedb.org/3/{search_type}/{tmdb_id}?api_key={TMDB_API_KEY}&append_to_response=videos"
            detail_res = requests.get(detail_url, timeout=10)
            detail_res.raise_for_status()
            res_json = detail_res.json()
            trailer_key = next((v['key'] for v in res_json.get("videos", {}).get("results", []) if v.get('type') == 'Trailer' and v.get('site') == 'YouTube'), None)
            details = {
                "tmdb_id": tmdb_id, "title": res_json.get("title") or res_json.get("name"), 
                "poster": f"https://image.tmdb.org/t/p/w500{res_json.get('poster_path')}" if res_json.get('poster_path') else None, 
                "overview": res_json.get("overview"), "release_date": res_json.get("release_date") or res_json.get("first_air_date"), 
                "genres": [g['name'] for g in res_json.get("genres", [])], "vote_average": res_json.get("vote_average"), "trailer_key": trailer_key
            }
            print(f"SUCCESS: Found TMDb details for '{query_title}' (ID: {tmdb_id}).")
            return details
        except requests.RequestException as e:
            print(f"ERROR: TMDb API request failed for '{query_title}'. Reason: {e}")
            return None
    tmdb_data = search_tmdb(title)
    if not tmdb_data and len(title.split()) > 1:
        simpler_title = " ".join(title.split()[:-1])
        print(f"INFO: Initial search failed. Retrying with simpler title: '{simpler_title}'")
        tmdb_data = search_tmdb(simpler_title)
    if not tmdb_data: print(f"WARNING: TMDb search found no results for '{title}' after all attempts.")
    return tmdb_data

def process_movie_list(movie_list):
    return [{**item, '_id': str(item['_id'])} for item in movie_list]

# ======================================================================
# --- Main Flask Routes ---
# ======================================================================

@app.route('/')
def home():
    query = request.args.get('q')
    if query:
        movies_list = list(movies.find({"title": {"$regex": query, "$options": "i"}}).sort('_id', -1))
        return render_template_string(index_html, movies=process_movie_list(movies_list), query=f'Results for "{query}"', is_full_page_list=True)
    limit = 12
    # For hero section, get one item with a valid poster
    hero_item_query = list(movies.find({"is_coming_soon": {"$ne": True}, "poster": {"$ne": None, "$not": /placeholder/}}).sort('_id', -1).limit(1))
    context = {
        "trending_movies": process_movie_list(list(movies.find({"is_trending": True, "is_coming_soon": {"$ne": True}}).sort('_id', -1).limit(limit))),
        "latest_movies": process_movie_list(list(movies.find({"type": "movie", "is_coming_soon": {"$ne": True}}).sort('_id', -1).limit(limit))),
        "latest_series": process_movie_list(list(movies.find({"type": "series", "is_coming_soon": {"$ne": True}}).sort('_id', -1).limit(limit))),
        "coming_soon_movies": process_movie_list(list(movies.find({"is_coming_soon": True}).sort('_id', -1).limit(limit))),
        "recently_added": process_movie_list(hero_item_query),
        "is_full_page_list": False, "query": ""
    }
    return render_template_string(index_html, **context)

@app.route('/movie/<movie_id>')
def movie_detail(movie_id):
    try:
        movie = movies.find_one({"_id": ObjectId(movie_id)})
        if not movie: return "Content not found", 404
        related_movies = []
        if movie.get("genres"):
            related_movies = list(movies.find({"genres": {"$in": movie["genres"]}, "_id": {"$ne": ObjectId(movie_id)}}).limit(6))
        return render_template_string(detail_html, movie=movie, trailer_key=movie.get("trailer_key"), related_movies=process_movie_list(related_movies))
    except Exception: return "Content not found", 404

@app.route('/watch/<movie_id>')
def watch_movie(movie_id):
    try:
        movie = movies.find_one({"_id": ObjectId(movie_id)})
        if not movie or not movie.get("watch_link"): return "Content not found.", 404
        return render_template_string(watch_html, watch_link=movie["watch_link"], title=movie["title"])
    except Exception: return "An error occurred.", 500

def render_full_list(content_list, title):
    return render_template_string(index_html, movies=process_movie_list(content_list), query=title, is_full_page_list=True)

@app.route('/badge/<badge_name>')
def movies_by_badge(badge_name): return render_full_list(list(movies.find({"poster_badge": badge_name}).sort('_id', -1)), f'Tag: {badge_name}')

@app.route('/genres')
def genres_page(): return render_template_string(genres_html, genres=sorted([g for g in movies.distinct("genres") if g]), title="Browse by Genre")

@app.route('/genre/<genre_name>')
def movies_by_genre(genre_name): return render_full_list(list(movies.find({"genres": genre_name}).sort('_id', -1)), f'Genre: {genre_name}')

@app.route('/trending_movies')
def trending_movies(): return render_full_list(list(movies.find({"is_trending": True, "is_coming_soon": {"$ne": True}}).sort('_id', -1)), "Trending Now")

@app.route('/movies_only')
def movies_only(): return render_full_list(list(movies.find({"type": "movie", "is_coming_soon": {"$ne": True}}).sort('_id', -1)), "All Movies")

@app.route('/webseries')
def webseries(): return render_full_list(list(movies.find({"type": "series", "is_coming_soon": {"$ne": True}}).sort('_id', -1)), "All Web Series")

@app.route('/coming_soon')
def coming_soon(): return render_full_list(list(movies.find({"is_coming_soon": True}).sort('_id', -1)), "Coming Soon")

@app.route('/recently_added')
def recently_added_all(): return render_full_list(list(movies.find({"is_coming_soon": {"$ne": True}}).sort('_id', -1)), "Recently Added")

# ======================================================================
# --- Admin and Webhook Routes ---
# ======================================================================
@app.route('/admin', methods=["GET", "POST"])
@requires_auth
def admin():
    if request.method == "POST":
        content_type = request.form.get("content_type", "movie")
        movie_data = {
            "title": request.form.get("title"),
            "type": content_type,
            "poster": request.form.get("poster") or PLACEHOLDER_POSTER,
            "overview": request.form.get("overview", ""),
            "is_trending": False,
            "is_coming_soon": False,
            "links": [], "files": [], "episodes": [], "season_packs": [], "languages": []
        }
        if content_type == "movie":
            movie_data['watch_link'] = request.form.get("watch_link")
        else:
            pass
        
        movies.insert_one(movie_data)
        return redirect(url_for('admin'))

    search_query = request.args.get('search', '').strip()
    query_filter = {}
    if search_query: query_filter = {"title": {"$regex": search_query, "$options": "i"}}
    ad_settings = settings.find_one() or {}
    content_list = process_movie_list(list(movies.find(query_filter).sort('_id', -1)))
    feedback_list = process_movie_list(list(feedback.find().sort('timestamp', -1)))
    return render_template_string(admin_html, content_list=content_list, ad_settings=ad_settings, feedback_list=feedback_list, search_query=search_query)


@app.route('/admin/save_ads', methods=['POST'])
@requires_auth
def save_ads():
    ad_codes = {
        "popunder_code": request.form.get("popunder_code", ""), "social_bar_code": request.form.get("social_bar_code", ""),
        "banner_ad_code": request.form.get("banner_ad_code", ""), "native_banner_code": request.form.get("native_banner_code", "")
    }
    settings.update_one({}, {"$set": ad_codes}, upsert=True)
    return redirect(url_for('admin'))

@app.route('/edit_movie/<movie_id>', methods=["GET", "POST"])
@requires_auth
def edit_movie(movie_id):
    try:
        obj_id = ObjectId(movie_id)
    except Exception:
        return "Invalid Movie ID", 400
    movie_obj = movies.find_one({"_id": obj_id})
    if not movie_obj: return "Movie not found", 404

    if request.method == "POST":
        content_type = request.form.get("content_type", "movie")
        update_data = {
            "title": request.form.get("title"), "type": content_type,
            "is_trending": request.form.get("is_trending") == "true",
            "is_coming_soon": request.form.get("is_coming_soon") == "true",
            "poster": request.form.get("poster", "").strip() or PLACEHOLDER_POSTER, 
            "overview": request.form.get("overview", "").strip(),
            "genres": [g.strip() for g in request.form.get("genres", "").split(',') if g.strip()],
            "languages": [lang.strip() for lang in request.form.get("languages", "").split(',') if lang.strip()],
            "poster_badge": request.form.get("poster_badge", "").strip() or None
        }
        
        if not movie_obj.get('tmdb_id'):
            tmdb_details = get_tmdb_details_from_api(update_data['title'], content_type)
            if tmdb_details:
                update_data.update(tmdb_details)

        if content_type == "movie":
            update_data["watch_link"] = request.form.get("watch_link", "")
            update_data["links"] = [{"quality": q, "url": u} for q, u in [("480p", request.form.get("link_480p")), ("720p", request.form.get("link_720p")), ("1080p", request.form.get("link_1080p"))] if u]
            update_data["files"] = [{"quality": q, "message_id": int(mid)} for q, mid in zip(request.form.getlist('telegram_quality[]'), request.form.getlist('telegram_message_id[]')) if q and mid]
            movies.update_one({"_id": obj_id}, {"$set": update_data, "$unset": {"episodes": "", "season_packs": ""}})
        else: # series
            update_data["episodes"] = [{"season": int(s), "episode_number": int(e), "title": t, "watch_link": w or None, "message_id": int(m) if m else None} for s, e, t, w, m in zip(request.form.getlist('episode_season[]'), request.form.getlist('episode_number[]'), request.form.getlist('episode_title[]'), request.form.getlist('episode_watch_link[]'), request.form.getlist('episode_message_id[]'))]
            
            update_data["season_packs"] = [
                {"season": int(s), "quality": q, "message_id": int(mid)} 
                for s, q, mid in zip(request.form.getlist('pack_season[]'), request.form.getlist('pack_quality[]'), request.form.getlist('pack_message_id[]')) 
                if s and q and mid
            ]
            
            movies.update_one({"_id": obj_id}, {"$set": update_data, "$unset": {"links": "", "watch_link": "", "files": ""}})
        
        return redirect(url_for('admin'))

    return render_template_string(edit_html, movie=movie_obj)


@app.route('/delete_movie/<movie_id>')
@requires_auth
def delete_movie(movie_id):
    movies.delete_one({"_id": ObjectId(movie_id)})
    return redirect(url_for('admin'))


@app.route('/admin/delete_all_movies')
@requires_auth
def delete_all_movies():
    try:
        result = movies.delete_many({})
        print(f"DELETED: {result.deleted_count} documents from the 'movies' collection by admin.")
    except Exception as e:
        print(f"ERROR: Could not delete all movies. Reason: {e}")
    return redirect(url_for('admin'))


@app.route('/contact', methods=['GET', 'POST'])
def contact():
    if request.method == 'POST':
        feedback_data = {
            "type": request.form.get("type"), "content_title": request.form.get("content_title"),
            "message": request.form.get("message"), "email": request.form.get("email", "").strip(),
            "reported_content_id": request.form.get("reported_content_id"), "timestamp": datetime.utcnow()
        }
        feedback.insert_one(feedback_data)
        return render_template_string(contact_html, message_sent=True)
    prefill_title, prefill_id = request.args.get('title', ''), request.args.get('report_id', '')
    prefill_type = 'Problem Report' if prefill_id else 'Movie Request'
    return render_template_string(contact_html, message_sent=False, prefill_title=prefill_title, prefill_id=prefill_id, prefill_type=prefill_type)


@app.route('/delete_feedback/<feedback_id>')
@requires_auth
def delete_feedback(feedback_id):
    feedback.delete_one({"_id": ObjectId(feedback_id)})
    return redirect(url_for('admin'))


@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    data = request.get_json()
    if 'channel_post' in data:
        post = data['channel_post']
        if str(post.get('chat', {}).get('id')) != ADMIN_CHANNEL_ID: 
            return jsonify(status='ok', reason='not_admin_channel')
        
        file = post.get('video') or post.get('document')
        if not (file and file.get('file_name')): 
            return jsonify(status='ok', reason='no_file_in_post')
        
        filename = file.get('file_name')
        print(f"\n--- [WEBHOOK] PROCESSING NEW FILE: {filename} ---")
        parsed_info = parse_filename(filename)
        
        if not parsed_info or not parsed_info.get('title'):
            print(f"FAILED: Could not parse title from filename: {filename}")
            return jsonify(status='ok', reason='parsing_failed')
        
        print(f"PARSED INFO: {parsed_info}")

        tmdb_data = get_tmdb_details_from_api(parsed_info['title'], parsed_info['type'], parsed_info.get('year'))

        def get_or_create_content_entry(tmdb_details, parsed_details):
            if tmdb_details and tmdb_details.get("tmdb_id"):
                print(f"INFO: TMDb data found for '{tmdb_details['title']}'. Processing with full details.")
                tmdb_id = tmdb_details.get("tmdb_id")
                existing_entry = movies.find_one({"tmdb_id": tmdb_id})
                if not existing_entry:
                    base_doc = {
                        **tmdb_details, "type": "movie" if parsed_details['type'] == 'movie' else "series",
                        "languages": [], "episodes": [], "season_packs": [], "files": [],
                        "is_trending": False, "is_coming_soon": False
                    }
                    movies.insert_one(base_doc)
                    newly_created_doc = movies.find_one({"tmdb_id": tmdb_id})
                    send_notification_to_channel(newly_created_doc)
                    return newly_created_doc
                return existing_entry
            else:
                print(f"WARNING: TMDb data not found for '{parsed_details['title']}'. Using/Creating a placeholder.")
                existing_entry = movies.find_one({
                    "title": {"$regex": f"^{re.escape(parsed_details['title'])}$", "$options": "i"}, 
                    "tmdb_id": None
                })
                if not existing_entry:
                    shell_doc = {
                        "title": parsed_details['title'], "type": "movie" if parsed_details['type'] == 'movie' else "series",
                        "poster": PLACEHOLDER_POSTER, "overview": "Details will be updated soon.",
                        "release_date": None, "genres": [], "vote_average": 0, "trailer_key": None, "tmdb_id": None,
                        "languages": [], "episodes": [], "season_packs": [], "files": [],
                        "is_trending": False, "is_coming_soon": False
                    }
                    movies.insert_one(shell_doc)
                    newly_created_doc = movies.find_one({"_id": shell_doc['_id']})
                    send_notification_to_channel(newly_created_doc)
                    return newly_created_doc
                return existing_entry

        content_entry = get_or_create_content_entry(tmdb_data, parsed_info)
        if not content_entry:
            print("FATAL: Could not get or create a content entry.")
            return jsonify(status="error", reason="db_entry_failed")

        update_op = {}
        if parsed_info.get('languages'):
            update_op["$addToSet"] = {"languages": {"$each": parsed_info['languages']}}

        if parsed_info['type'] == 'movie':
            quality = parsed_info.get('quality', 'HD')
            new_file = {"quality": quality, "message_id": post['message_id']}
            movies.update_one({"_id": content_entry['_id']}, {"$pull": {"files": {"quality": new_file['quality']}}})
            update_op.setdefault("$push", {})["files"] = new_file
        
        elif parsed_info['type'] == 'series_pack':
            new_pack = {"season": parsed_info['season'], "quality": parsed_info['quality'], "message_id": post['message_id']}
            movies.update_one(
                {"_id": content_entry['_id']}, 
                {"$pull": {"season_packs": {"season": new_pack['season'], "quality": new_pack['quality']}}}
            )
            update_op.setdefault("$push", {})["season_packs"] = new_pack

        elif parsed_info['type'] == 'series':
            new_episode = {"season": parsed_info['season'], "episode_number": parsed_info['episode'], "message_id": post['message_id']}
            movies.update_one(
                {"_id": content_entry['_id']},
                {"$pull": {"episodes": {"season": new_episode['season'], "episode_number": new_episode['episode_number']}}}
            )
            update_op.setdefault("$push", {})["episodes"] = new_episode

        if update_op:
            movies.update_one({"_id": content_entry['_id']}, update_op)
            print(f"SUCCESS: Entry for '{content_entry['title']}' has been updated.")

    elif 'message' in data:
        message = data['message']
        chat_id = message['chat']['id']
        text = message.get('text', '')
        if text.startswith('/start'):
            parts = text.split()
            if len(parts) > 1:
                try:
                    payload_parts = parts[1].split('_')
                    doc_id_str = payload_parts[0]
                    content = movies.find_one({"_id": ObjectId(doc_id_str)})
                    if not content: return jsonify(status='ok')

                    message_to_copy_id = None
                    file_info_text = ""
                    
                    if len(payload_parts) == 3 and payload_parts[1].startswith('S'): # Season Pack
                        season_num_str = payload_parts[1][1:]
                        season_num = int(season_num_str)
                        quality = payload_parts[2]
                        pack = next((p for p in content.get('season_packs', []) if p.get('season') == season_num and p.get('quality') == quality), None)
                        if pack:
                            message_to_copy_id = pack.get('message_id')
                            file_info_text = f"Complete Season {season_num} ({quality})"

                    elif content.get('type') == 'series' and len(payload_parts) == 3: # Episodic
                        s_num, e_num = int(payload_parts[1]), int(payload_parts[2])
                        episode = next((ep for ep in content.get('episodes', []) if ep.get('season') == s_num and ep.get('episode_number') == e_num), None)
                        if episode: 
                            message_to_copy_id = episode.get('message_id')
                            file_info_text = f"S{s_num:02d}E{e_num:02d}"

                    elif content.get('type') == 'movie' and len(payload_parts) == 2: # Movie
                        quality = payload_parts[1]
                        file = next((f for f in content.get('files', []) if f.get('quality') == quality), None)
                        if file: 
                            message_to_copy_id = file.get('message_id')
                            file_info_text = f"({quality})"
                    
                    if message_to_copy_id:
                        caption_text = (
                            f"🎬 *{escape_markdown(content['title'])}* {escape_markdown(file_info_text)}\n\n"
                            f"✅ *Successfully Sent To Your PM*\n\n"
                            f"🔰 Join Our Main Channel\n➡️ [{escape_markdown(BOT_USERNAME)} Main]({MAIN_CHANNEL_LINK})\n\n"
                            f"📢 Join Our Update Channel\n➡️ [{escape_markdown(BOT_USERNAME)} Official]({UPDATE_CHANNEL_LINK})\n\n"
                            f"💬 For Any Help or Request\n➡️ [Contact Developer]({DEVELOPER_USER_LINK})"
                        )
                        payload = {'chat_id': chat_id, 'from_chat_id': ADMIN_CHANNEL_ID, 'message_id': message_to_copy_id, 'caption': caption_text, 'parse_mode': 'MarkdownV2'}
                        res = requests.post(f"{TELEGRAM_API_URL}/copyMessage", json=payload).json()
                        
                        if res.get('ok'):
                            new_msg_id = res['result']['message_id']
                            scheduler.add_job(func=delete_message_after_delay, trigger='date', run_date=datetime.now() + timedelta(minutes=30), args=[chat_id, new_msg_id], id=f'del_{chat_id}_{new_msg_id}', replace_existing=True)
                        else: 
                            requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': "Error sending file. It might have been deleted from the channel."})
                    else: 
                        requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': "Requested file/season not found."})
                except Exception as e:
                    print(f"Error processing /start command: {e}")
                    requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': "An unexpected error occurred."})
            else: 
                welcome_message = (f"👋 Welcome to {BOT_USERNAME}!\n\nBrowse all our content on our website.")
                try:
                    root_url = url_for('home', _external=True)
                    keyboard = {"inline_keyboard": [[{"text": "🎬 Visit Website", "url": root_url}]]}
                    requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': welcome_message, 'reply_markup': str(keyboard).replace("'", '"')})
                except Exception:
                     requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': welcome_message})

    return jsonify(status='ok')


@app.route('/notify/<movie_id>')
@requires_auth
def send_manual_notification(movie_id):
    """
    অ্যাডমিন প্যানেল থেকে যেকোনো পুরোনো মুভির জন্য নোটিফিকেশন পাঠায়।
    """
    try:
        obj_id = ObjectId(movie_id)
        movie_obj = movies.find_one({"_id": obj_id})
        
        if movie_obj:
            print(f"ADMIN_ACTION: Manually triggering notification for '{movie_obj.get('title')}'")
            send_notification_to_channel(movie_obj)
        else:
            print(f"ADMIN_ACTION_FAIL: Could not find movie with ID {movie_id} to send notification.")
            
    except Exception as e:
        print(f"ERROR in send_manual_notification for ID {movie_id}: {e}")
        
    return redirect(url_for('admin'))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
