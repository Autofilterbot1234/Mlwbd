import os
import sys
import re
import requests
import json
from flask import Flask, render_template_string, request, redirect, url_for, Response, jsonify
from pymongo import MongoClient
from bson.objectid import ObjectId
from functools import wraps
from datetime import datetime, timedelta, timezone
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
ADMIN_USER_IDS_STR = os.environ.get("ADMIN_USER_IDS") 
ADMIN_USER_IDS = [uid.strip() for uid in ADMIN_USER_IDS_STR.split(',')] if ADMIN_USER_IDS_STR else []
MAIN_CHANNEL_LINK = os.environ.get("MAIN_CHANNEL_LINK")
UPDATE_CHANNEL_LINK = os.environ.get("UPDATE_CHANNEL_LINK")
DEVELOPER_USER_LINK = os.environ.get("DEVELOPER_USER_LINK")
PUBLIC_CHANNEL_ID = os.environ.get("PUBLIC_CHANNEL_ID")
WEBSITE_URL = os.environ.get("WEBSITE_URL")

required_vars = {
    "MONGO_URI": MONGO_URI, "BOT_TOKEN": BOT_TOKEN, "TMDB_API_KEY": TMDB_API_KEY,
    "ADMIN_CHANNEL_ID": ADMIN_CHANNEL_ID, "BOT_USERNAME": BOT_USERNAME,
    "ADMIN_USERNAME": ADMIN_USERNAME, "ADMIN_PASSWORD": ADMIN_PASSWORD,
    "ADMIN_USER_IDS": ADMIN_USER_IDS_STR,
    "MAIN_CHANNEL_LINK": MAIN_CHANNEL_LINK,
    "UPDATE_CHANNEL_LINK": UPDATE_CHANNEL_LINK,
    "DEVELOPER_USER_LINK": DEVELOPER_USER_LINK,
    "PUBLIC_CHANNEL_ID": PUBLIC_CHANNEL_ID,
    "WEBSITE_URL": WEBSITE_URL,
}
missing_vars = [name for name, value in required_vars.items() if not value]
if missing_vars:
    print(f"FATAL: Missing required environment variables: {', '.join(missing_vars)}")
    sys.exit(1)

# ======================================================================
# --- অ্যাপ্লিকেশন সেটআপ এবং অন্যান্য ফাংশন ---
# ======================================================================
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
app = Flask(__name__)

def check_auth(username, password): return username == ADMIN_USERNAME and password == ADMIN_PASSWORD
def authenticate(): return Response('Could not verify your access level.', 401, {'WWW-Authenticate': 'Basic realm="Login Required"'})

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
    movies, settings, feedback = db["movies"], db["settings"], db["feedback"]
    print("SUCCESS: Successfully connected to MongoDB!")
except Exception as e:
    print(f"FATAL: Error connecting to MongoDB: {e}. Exiting.")
    sys.exit(1)

@app.context_processor
def inject_global_vars():
    ad_codes = settings.find_one() or {}
    
    def format_links_for_edit(links_list):
        if not links_list or not isinstance(links_list, list): return ""
        return ", ".join([f"{link.get('lang', 'Link')}: {link.get('url', '')}" for link in links_list])

    return dict(
        ad_settings=ad_codes, 
        bot_username=BOT_USERNAME, 
        main_channel_link=MAIN_CHANNEL_LINK, 
        format_links_for_edit=format_links_for_edit
    )

def escape_markdown(text: str) -> str:
    if not isinstance(text, str): return ''
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def parse_links_from_string(link_string: str) -> list:
    if not link_string or not link_string.strip(): return []
    links = []
    parts = [p.strip() for p in link_string.split(',') if p.strip()]
    for part in parts:
        if 'msgid:' in part.lower(): # Special handling for message ID
            try:
                msg_id = int(part.split(':', 1)[1].strip())
                links.append({'lang': 'Telegram', 'message_id': msg_id}) # Storing as message_id for internal use
            except ValueError:
                print(f"WARNING: Invalid Message ID format in link string: {part}")
                links.append({'lang': 'Link', 'url': part}) # Fallback to URL if MsgID is invalid
        elif ':' in part:
            try:
                lang, url = part.split(':', 1)
                links.append({'lang': lang.strip().title(), 'url': url.strip()})
            except ValueError:
                links.append({'lang': 'Link', 'url': part})
        else:
            links.append({'lang': 'Link', 'url': part})
    return links

# Helper function to parse complex series episode/pack strings
def parse_series_links_from_string(series_link_str: str, is_pack=False) -> list:
    """
    Parses a string like "S1:WatchUrl,DlUrl;S2:MsgId:12345" for season packs
    or "S1E1:WatchUrl,DlUrl;S1E2(Title):MsgId:67890" for episodes.
    """
    if not series_link_str or not series_link_str.strip():
        return []

    parsed_items = []
    items = [item.strip() for item in series_link_str.split(';') if item.strip()]

    for item_str in items:
        try:
            # Separate the S/E part and the links part
            se_part_match = re.match(r'(S\d+(?:E\d+)?(?:\s*\(.*?\))?)\s*:\s*(.*)', item_str, re.IGNORECASE)
            if not se_part_match:
                print(f"WARNING: Invalid S/E part format: {item_str}")
                continue
            
            se_identifier = se_part_match.group(1).strip()
            links_raw = se_part_match.group(2).strip()

            item_data = {}
            if is_pack: # Season Pack
                pack_match = re.match(r'S(\d+)', se_identifier, re.IGNORECASE)
                if not pack_match:
                    print(f"WARNING: Invalid season pack identifier: {se_identifier}")
                    continue
                item_data['season'] = int(pack_match.group(1))
            else: # Episode
                ep_match = re.match(r'S(\d+)E(\d+)(?:\s*\((.*?)\))?', se_identifier, re.IGNORECASE)
                if not ep_match:
                    print(f"WARNING: Invalid episode identifier: {se_identifier}")
                    continue
                item_data['season'] = int(ep_match.group(1))
                item_data['episode_number'] = int(ep_match.group(2))
                item_data['title'] = ep_match.group(3) if ep_match.group(3) else f"Episode {item_data['episode_number']}"
            
            # Parse actual links or message ID
            if links_raw.lower().startswith('msgid:'):
                try:
                    msg_id = int(links_raw.split(':', 1)[1].strip())
                    item_data['message_id'] = msg_id
                    item_data['watch_links'] = []
                    item_data['download_links'] = []
                except ValueError:
                    print(f"WARNING: Invalid Message ID in {se_identifier}: {links_raw}")
                    continue
            else:
                link_parts = [p.strip() for p in links_raw.split(',') if p.strip()]
                watch_links = []
                download_links = []
                # Assuming the first link is watch, second is download if only two are given
                # Or if explicitly specified like "Watch:url,Download:url"
                for idx, link_part in enumerate(link_parts):
                    if ':' in link_part:
                        lang_type, url = link_part.split(':', 1)
                        if lang_type.lower() == 'watch':
                            watch_links.append({'lang': 'Link', 'url': url.strip()}) # General link type
                        elif lang_type.lower() == 'download':
                            download_links.append({'lang': 'Link', 'url': url.strip()})
                        else: # Assume it's a language specific link
                            watch_links.append({'lang': lang_type.strip().title(), 'url': url.strip()})
                    else: # Assume general link, add to watch links
                        if idx == 0: watch_links.append({'lang': 'Link', 'url': link_part})
                        else: download_links.append({'lang': 'Link', 'url': link_part})
                item_data['watch_links'] = watch_links
                item_data['download_links'] = download_links
                item_data['message_id'] = None

            parsed_items.append(item_data)
        except Exception as e:
            print(f"ERROR: Failed to parse series item '{item_str}'. Error: {e}")
            continue
    return parsed_items

# ======================================================================
# --- উন্নত ফাংশন: পাবলিক চ্যানেলে পোস্ট করার জন্য ---
# ======================================================================
def post_to_public_channel(content_id, post_type='content', season_num=None):
    if not PUBLIC_CHANNEL_ID or not WEBSITE_URL:
        print("WARNING: PUBLIC_CHANNEL_ID or WEBSITE_URL is not set. Skipping public post.")
        return

    try:
        content = movies.find_one({"_id": ObjectId(content_id)})
        if not content:
            print(f"ERROR: Could not find content with ID {content_id} to post.")
            return

        title = content.get('title', 'No Title')
        poster_url = content.get('poster')
        genres = content.get('genres', [])
        rating = content.get('vote_average')
        release_date = content.get('release_date')
        
        escaped_title = escape_markdown(title)
        
        caption_parts = [f"🎬 *{escaped_title}*"]

        if release_date:
            year = release_date.split('-')[0]
            caption_parts.append(f"🗓️ *Release Year:* {escape_markdown(year)}")
            
        if genres:
            escaped_genres = escape_markdown(", ".join(genres))
            caption_parts.append(f"🎭 *Genre:* {escaped_genres}")

        if post_type == 'season_pack' and season_num:
            caption_parts.insert(1, f"🔥 *Season {season_num} Pack Added*")
            pack = next((p for p in content.get('season_packs', []) if p['season'] == season_num), None)
            pack_langs = set()
            if pack:
                for link in pack.get('watch_links', []) + pack.get('download_links', []):
                    lang = link.get('lang', 'N/A').strip()
                    if lang and lang != 'N/A': pack_langs.add(lang)
            languages_str = ", ".join(sorted(list(pack_langs))) or "Not Specified"
            if languages_str != "Not Specified":
                caption_parts.append(f"🗣️ *Language:* {escape_markdown(languages_str)}")
        else:
            languages = content.get('languages', [])
            if languages:
                 escaped_langs = escape_markdown(", ".join(languages))
                 caption_parts.append(f"🗣️ *Language:* {escaped_langs}")

        if rating and float(rating) > 0:
            escaped_rating = escape_markdown(f"{rating:.1f}/10")
            caption_parts.append(f"⭐ *Rating:* {escaped_rating}")

        caption = "\n\n".join(caption_parts)

        with app.app_context():
            website_link = f"{WEBSITE_URL.rstrip('/')}{url_for('movie_detail', movie_id=str(content_id))}"
        
        keyboard = { "inline_keyboard": [[{"text": "🌐 Watch on Website", "url": website_link}]] }

        if poster_url:
            payload = {'chat_id': PUBLIC_CHANNEL_ID, 'photo': poster_url, 'caption': caption, 'parse_mode': 'MarkdownV2', 'reply_markup': json.dumps(keyboard)}
            response = requests.post(f"{TELEGRAM_API_URL}/sendPhoto", json=payload)
        else:
            payload = {'chat_id': PUBLIC_CHANNEL_ID, 'text': caption, 'parse_mode': 'MarkdownV2', 'reply_markup': json.dumps(keyboard)}
            response = requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload)

        if response.status_code == 200:
            print(f"SUCCESS: Successfully posted '{title}' (Type: {post_type}) to public channel.")
        else:
            print(f"ERROR: Failed to post to public channel. Status: {response.status_code}, Response: {response.text}")

    except Exception as e:
        print(f"FATAL ERROR in post_to_public_channel: {e}")

# ======================================================================
# --- HTML টেমপ্লেট ---
# ======================================================================
index_html = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no" />
<title>MovieZone - Your Entertainment Hub</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Roboto:wght@400;500;700&display=swap');
  :root { --netflix-red: #E50914; --netflix-black: #141414; --text-light: #f5f5f5; --text-dark: #a0a0a0; --nav-height: 60px; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Roboto', sans-serif; background-color: var(--netflix-black); color: var(--text-light); overflow-x: hidden; }
  a { text-decoration: none; color: inherit; }
  ::-webkit-scrollbar { width: 8px; } ::-webkit-scrollbar-track { background: #222; } ::-webkit-scrollbar-thumb { background: #555; } ::-webkit-scrollbar-thumb:hover { background: var(--netflix-red); }
  .main-nav { position: fixed; top: 0; left: 0; width: 100%; padding: 15px 50px; display: flex; justify-content: space-between; align-items: center; z-index: 100; transition: background-color 0.3s ease; background: linear-gradient(to bottom, rgba(0,0,0,0.8) 10%, rgba(0,0,0,0)); }
  .main-nav.scrolled { background-color: var(--netflix-black); }
  .logo { font-family: 'Bebas Neue', sans-serif; font-size: 32px; color: var(--netflix-red); font-weight: 700; letter-spacing: 1px; }
  .search-input { background-color: rgba(0,0,0,0.7); border: 1px solid #777; color: var(--text-light); padding: 8px 15px; border-radius: 4px; transition: width 0.3s ease, background-color 0.3s ease; width: 250px; }
  .search-input:focus { background-color: rgba(0,0,0,0.9); border-color: var(--text-light); outline: none; }
  .tags-section { padding: 80px 50px 20px 50px; background-color: var(--netflix-black); }
  .tags-container { display: flex; flex-wrap: wrap; justify-content: center; gap: 10px; }
  .tag-link { padding: 6px 16px; background-color: rgba(255, 255, 255, 0.1); border: 1px solid #444; border-radius: 50px; font-weight: 500; font-size: 0.85rem; transition: all 0.3s; }
  .tag-link:hover { background-color: var(--netflix-red); border-color: var(--netflix-red); color: white; }
  .hero-section { height: 85vh; position: relative; color: white; overflow: hidden; }
  .hero-slide { position: absolute; top: 0; left: 0; width: 100%; height: 100%; background-size: cover; background-position: center top; display: flex; align-items: flex-end; padding: 50px; opacity: 0; transition: opacity 1.5s ease-in-out; z-index: 1; }
  .hero-slide.active { opacity: 1; z-index: 2; }
  .hero-slide::before { content: ''; position: absolute; top: 0; left: 0; right: 0; bottom: 0; background: linear-gradient(to top, var(--netflix-black) 10%, transparent 50%), linear-gradient(to right, rgba(0,0,0,0.8) 0%, transparent 60%); }
  .hero-content { position: relative; z-index: 3; max-width: 50%; }
  .hero-title { font-family: 'Bebas Neue', sans-serif; font-size: 5rem; font-weight: 700; margin-bottom: 1rem; line-height: 1; }
  .hero-overview { font-size: 1.1rem; line-height: 1.5; margin-bottom: 1.5rem; max-width: 600px; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
  .hero-buttons .btn { padding: 10px 24px; margin-right: 0.8rem; border: none; border-radius: 4px; font-size: 1rem; font-weight: 700; cursor: pointer; transition: opacity 0.3s ease; display: inline-flex; align-items: center; gap: 8px; }
  .btn.btn-primary { background-color: var(--netflix-red); color: white; } .btn.btn-secondary { background-color: rgba(109, 109, 110, 0.7); color: white; } .btn:hover { opacity: 0.8; }
  main { padding: 0 50px; }
  .movie-card { display: block; cursor: pointer; transition: transform 0.3s ease; }
  .poster-wrapper { position: relative; width: 100%; border-radius: 6px; overflow: hidden; background-color: #222; display: flex; flex-direction: column; }
  .movie-poster-container { position: relative; overflow: hidden; width:100%; flex-grow:1; aspect-ratio: 2 / 3; }
  .movie-poster { width: 100%; height: 100%; object-fit: cover; display: block; transition: transform 0.4s ease; }
  @keyframes rgb-glow {
    0%, 100% { color: #ff5555; text-shadow: 0 0 5px #ff5555, 0 0 10px #ff5555; }
    33% { color: #55ff55; text-shadow: 0 0 5px #55ff55, 0 0 10px #55ff55; }
    66% { color: #55aaff; text-shadow: 0 0 5px #55aaff, 0 0 10px #55aaff; }
  }
  .poster-badge {
    position: absolute; top: 18px; left: -35px; width: 140px; background: rgba(20, 20, 20, 0.8);
    backdrop-filter: blur(5px); transform: rotate(-45deg); text-align: center; z-index: 5;
    font-size: 0.75rem; font-weight: 700; padding: 4px 0; border: 1px solid rgba(255, 255, 255, 0.2);
    animation: rgb-glow 3s linear infinite;
  }
  .rating-badge { position: absolute; top: 10px; right: 10px; background-color: rgba(0, 0, 0, 0.8); color: white; padding: 5px 10px; font-size: 0.8rem; font-weight: 700; border-radius: 20px; z-index: 3; display: flex; align-items: center; gap: 5px; backdrop-filter: blur(5px); }
  .rating-badge .fa-star { color: #f5c518; }
  .card-info-static { padding: 10px 8px; background-color: #1a1a1a; text-align: left; width: 100%; flex-shrink: 0; }
  .card-info-title { font-size: 0.9rem; font-weight: 500; color: var(--text-light); margin: 0 0 4px 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .card-info-meta { font-size: 0.75rem; color: var(--text-dark); margin: 0; }
  @media (hover: hover) { .movie-card:hover { transform: scale(1.05); z-index: 10; box-shadow: 0 0 20px rgba(229, 9, 20, 0.5); } .movie-card:hover .movie-poster { transform: scale(1.1); } }
  .full-page-grid-container { padding-top: 100px; padding-bottom: 50px; }
  .full-page-grid-title { font-size: 2.5rem; font-weight: 700; margin-bottom: 30px; }
  .category-grid, .full-page-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 20px 15px; }
  .category-section { margin: 40px 0; }
  .category-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; }
  .category-title { font-family: 'Roboto', sans-serif; font-weight: 700; font-size: 1.6rem; margin: 0; }
  .see-all-link { color: var(--text-dark); font-weight: 700; font-size: 0.9rem; }
  .bottom-nav { display: none; position: fixed; bottom: 0; left: 0; right: 0; height: var(--nav-height); background-color: #181818; border-top: 1px solid #282828; justify-content: space-around; align-items: center; z-index: 200; }
  .nav-item { display: flex; flex-direction: column; align-items: center; color: var(--text-dark); font-size: 10px; flex-grow: 1; padding: 5px 0; transition: color 0.2s ease; }
  .nav-item i { font-size: 20px; margin-bottom: 4px; } .nav-item.active { color: var(--text-light); } .nav-item.active i { color: var(--netflix-red); }
  .ad-container { margin: 40px 0; display: flex; justify-content: center; align-items: center; }
  .telegram-join-section { background-color: #181818; padding: 40px 20px; text-align: center; margin: 50px -50px -50px -50px; }
  .telegram-join-section .telegram-icon { font-size: 4rem; color: #2AABEE; margin-bottom: 15px; } .telegram-join-section h2 { font-family: 'Bebas Neue', sans-serif; font-size: 2.5rem; color: var(--text-light); margin-bottom: 10px; }
  .telegram-join-section p { font-size: 1.1rem; color: var(--text-dark); max-width: 600px; margin: 0 auto 25px auto; }
  .telegram-join-button { display: inline-flex; align-items: center; gap: 10px; background-color: #2AABEE; color: white; padding: 12px 30px; border-radius: 50px; font-size: 1.1rem; font-weight: 700; transition: all 0.2s ease; }
  .telegram-join-button:hover { transform: scale(1.05); background-color: #1e96d1; } .telegram-join-button i { font-size: 1.3rem; }
  @media (max-width: 768px) {
      body { padding-bottom: var(--nav-height); } .main-nav { padding: 10px 15px; } main { padding: 0 15px; } .logo { font-size: 24px; } .search-input { width: 150px; }
      .tags-section { padding: 80px 15px 15px 15px; } .tag-link { padding: 6px 15px; font-size: 0.8rem; } .hero-section { height: 60vh; margin: 0 -15px;}
      .hero-slide { padding: 15px; align-items: center; } .hero-content { max-width: 90%; text-align: center; } .hero-title { font-size: 2.8rem; } .hero-overview { display: none; }
      .category-section { margin: 25px 0; } .category-title { font-size: 1.2rem; }
      .category-grid, .full-page-grid { grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 15px 10px; }
      .full-page-grid-container { padding-top: 80px; } .full-page-grid-title { font-size: 1.8rem; }
      .bottom-nav { display: flex; } .ad-container { margin: 25px 0; }
      .telegram-join-section { margin: 50px -15px -30px -15px; }
      .telegram-join-section h2 { font-size: 2rem; } .telegram-join-section p { font-size: 1rem; }
  }
</style>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.2.0/css/all.min.css">
</head>
<body>
<header class="main-nav"><a href="{{ url_for('home') }}" class="logo">MovieZone</a><form method="GET" action="/" class="search-form"><input type="search" name="q" class="search-input" placeholder="Search..." value="{{ query|default('') }}" /></form></header>
<main>
  {% macro render_movie_card(m) %}
    <a href="{{ url_for('movie_detail', movie_id=m._id) }}" class="movie-card">
      <div class="poster-wrapper">
        <div class="movie-poster-container">
           <img class="movie-poster" loading="lazy" src="{{ m.poster or 'https://via.placeholder.com/400x600.png?text=No+Image' }}" alt="{{ m.title }}">
           {% if m.poster_badge %}<div class="poster-badge">{{ m.poster_badge }}</div>{% endif %}
           {% if m.vote_average and m.vote_average > 0 %}<div class="rating-badge"><i class="fas fa-star"></i> {{ "%.1f"|format(m.vote_average) }}</div>{% endif %}
        </div>
        <div class="card-info-static">
          <h4 class="card-info-title">{{ m.title }}</h4>
          {% if m.release_date %}<p class="card-info-meta">{{ m.release_date.split('-')[0] }}</p>{% endif %}
        </div>
      </div>
    </a>
  {% endmacro %}

  {% if is_full_page_list %}
    <div class="full-page-grid-container">
        <h2 class="full-page-grid-title">{{ query }}</h2>
        {% if movies|length == 0 %}
            <p style="text-align:center; color: var(--text-dark); margin-top: 40px;">No content found.</p>
        {% else %}
            <div class="full-page-grid">
                {% for m in movies %}
                    {{ render_movie_card(m) }}
                {% endfor %}
            </div>
        {% endif %}
    </div>
  {% else %}
    {% if all_badges %}<div class="tags-section"><div class="tags-container">{% for badge in all_badges %}<a href="{{ url_for('movies_by_badge', badge_name=badge) }}" class="tag-link">{{ badge }}</a>{% endfor %}</div></div>{% endif %}
    
    {% if recently_added %}<div class="hero-section">{% for movie in recently_added %}<div class="hero-slide {% if loop.first %}active{% endif %}" style="background-image: url('{{ movie.poster or '' }}');"><div class="hero-content"><h1 class="hero-title">{{ movie.title }}</h1><p class="hero-overview">{{ movie.overview }}</p><div class="hero-buttons">{% if movie.watch_links and movie.watch_links[0] and not movie.is_coming_soon %}<a href="{{ movie.watch_links[0].url }}" class="btn btn-primary"><i class="fas fa-play"></i> Watch Now</a>{% endif %}<a href="{{ url_for('movie_detail', movie_id=movie._id) }}" class="btn btn-secondary"><i class="fas fa-info-circle"></i> More Info</a></div></div></div>{% endfor %}</div>{% endif %}

    {% macro render_grid_section(title, movies_list, endpoint) %}
        {% if movies_list %}
        <div class="category-section">
            <div class="category-header">
                <h2 class="category-title">{{ title }}</h2>
                <a href="{{ url_for(endpoint) }}" class="see-all-link">See All ></a>
            </div>
            <div class="category-grid">
                {% for m in movies_list %}
                    {{ render_movie_card(m) }}
                {% endfor %}
            </div>
        </div>
        {% endif %}
    {% endmacro %}

    {{ render_grid_section('Trending Now', trending_movies, 'trending_movies') }}
    {% if ad_settings.banner_ad_code %}<div class="ad-container">{{ ad_settings.banner_ad_code|safe }}</div>{% endif %}
    {{ render_grid_section('Latest Movies', latest_movies, 'movies_only') }}
    {% if ad_settings.native_banner_code %}<div class="ad-container">{{ ad_settings.native_banner_code|safe }}</div>{% endif %}
    {{ render_grid_section('Web Series', latest_series, 'webseries') }}
    {{ render_grid_section('Recently Added', recently_added_full, 'recently_added_all') }}
    {{ render_grid_section('Coming Soon', coming_soon_movies, 'coming_soon') }}
    
    <div class="telegram-join-section">
        <i class="fa-brands fa-telegram telegram-icon"></i>
        <h2>Join Our Telegram Channel</h2>
        <p>Get the latest movie updates, news, and direct download links right on your phone!</p>
        <a href="{{ main_channel_link or '#' }}" target="_blank" class="telegram-join-button"><i class="fa-brands fa-telegram"></i> Join Main Channel</a>
    </div>
  {% endif %}
</main>
<nav class="bottom-nav">
    <a href="{{ url_for('home') }}" class="nav-item {% if request.endpoint == 'home' %}active{% endif %}">
        <i class="fas fa-home"></i><span>Home</span>
    </a>
    <a href="{{ url_for('movies_only') }}" class="nav-item {% if request.endpoint == 'movies_only' %}active{% endif %}">
        <i class="fas fa-film"></i><span>Movies</span>
    </a>
    <a href="{{ url_for('webseries') }}" class="nav-item {% if request.endpoint == 'webseries' %}active{% endif %}">
        <i class="fas fa-tv"></i><span>Series</span>
    </a>
    <a href="{{ url_for('genres_page') }}" class="nav-item {% if request.endpoint == 'genres_page' %}active{% endif %}">
        <i class="fas fa-layer-group"></i><span>Genres</span>
    </a>
    <a href="{{ url_for('contact') }}" class="nav-item {% if request.endpoint == 'contact' %}active{% endif %}">
        <i class="fas fa-envelope"></i><span>Request</span>
    </a>
</nav>
<script>
    const nav = document.querySelector('.main-nav');
    window.addEventListener('scroll', () => { window.scrollY > 50 ? nav.classList.add('scrolled') : nav.classList.remove('scrolled'); });
    document.addEventListener('DOMContentLoaded', function() { const slides = document.querySelectorAll('.hero-slide'); if (slides.length > 1) { let currentSlide = 0; const showSlide = (index) => slides.forEach((s, i) => s.classList.toggle('active', i === index)); setInterval(() => { currentSlide = (currentSlide + 1) % slides.length; showSlide(currentSlide); }, 5000); } });
</script>
{% if ad_settings.popunder_code %}{{ ad_settings.popunder_code|safe }}{% endif %}
{% if ad_settings.social_bar_code %}{{ ad_settings.social_bar_code|safe }}{% endif %}
</body>
</html>
"""

detail_html = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no" />
<title>{{ movie.title if movie else "Content Not Found" }} - MovieZone</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Roboto:wght@400;500;700&display=swap');
  :root { --netflix-red: #E50914; --netflix-black: #141414; --text-light: #f5f5f5; --text-dark: #a0a0a0; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Roboto', sans-serif; background: var(--netflix-black); color: var(--text-light); }
  .detail-header { position: absolute; top: 0; left: 0; right: 0; padding: 20px 50px; z-index: 100; }
  .back-button { color: var(--text-light); font-size: 1.2rem; font-weight: 700; text-decoration: none; display: flex; align-items: center; gap: 10px; transition: color 0.3s ease; }
  .back-button:hover { color: var(--netflix-red); }
  .detail-hero { position: relative; width: 100%; display: flex; align-items: center; justify-content: center; padding: 100px 0; }
  .detail-hero-background { position: absolute; top: 0; left: 0; right: 0; bottom: 0; background-size: cover; background-position: center; filter: blur(20px) brightness(0.4); transform: scale(1.1); }
  .detail-hero::after { content: ''; position: absolute; top: 0; left: 0; right: 0; bottom: 0; background: linear-gradient(to top, rgba(20,20,20,1) 0%, rgba(20,20,20,0.6) 50%, rgba(20,20,20,1) 100%); }
  .detail-content-wrapper { position: relative; z-index: 2; display: flex; gap: 40px; max-width: 1200px; padding: 0 50px; width: 100%; }
  .detail-poster { width: 300px; height: 450px; flex-shrink: 0; border-radius: 8px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); object-fit: cover; }
  .detail-info { flex-grow: 1; max-width: 65%; }
  .detail-title { font-family: 'Bebas Neue', sans-serif; font-size: 4.5rem; font-weight: 700; line-height: 1.1; margin-bottom: 20px; }
  .detail-meta { display: flex; flex-wrap: wrap; gap: 20px; margin-bottom: 25px; font-size: 1rem; color: var(--text-dark); }
  .detail-meta span { font-weight: 700; color: var(--text-light); }
  .detail-meta span i { margin-right: 5px; color: var(--text-dark); }
  .detail-overview { font-size: 1.1rem; line-height: 1.6; margin-bottom: 30px; }
  
  .action-buttons-container { display: flex; flex-wrap: wrap; gap: 15px; margin-bottom: 15px; }
  .action-btn { background-color: var(--netflix-red); color: white; padding: 12px 25px; font-size: 1rem; font-weight: 700; border: none; border-radius: 5px; cursor: pointer; display: inline-flex; align-items: center; gap: 10px; text-decoration: none; transition: all 0.2s ease; justify-content: center; }
  .action-btn.download { background-color: #3b82f6; }
  .action-btn:hover { transform: scale(1.02); filter: brightness(1.1); }

  .section-title { font-size: 1.5rem; font-weight: 700; margin-bottom: 20px; padding-bottom: 5px; border-bottom: 2px solid var(--netflix-red); display: inline-block; }
  .video-container { position: relative; padding-bottom: 56.25%; height: 0; overflow: hidden; max-width: 100%; background: #000; border-radius: 8px; }
  .video-container iframe { position: absolute; top: 0; left: 0; width: 100%; height: 100%; }
  .download-section, .episode-section { margin-top: 30px; }
  .download-button { display: inline-block; padding: 12px 25px; background-color: #444; color: white; text-decoration: none; border-radius: 4px; font-weight: 700; transition: background-color 0.3s ease; margin-right: 10px; margin-bottom: 10px; text-align: center; vertical-align: middle; }
  .episode-item { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; padding: 15px; border-radius: 5px; background-color: #1a1a1a; border-left: 4px solid var(--netflix-red); }
  .episode-title { font-size: 1.1rem; font-weight: 500; color: #fff; flex-grow: 1; }
  .episode-buttons { display: flex; gap: 10px; flex-shrink: 0; }
  .episode-button { display: inline-flex; align-items:center; gap: 8px; padding: 10px 20px; background-color: #444; color: white; text-decoration: none; border-radius: 4px; font-weight: 700; font-size: 0.9rem; transition: background-color 0.3s ease; }
  .episode-button.download { background-color: #3b82f6; }
  .episode-button.telegram { background-color: #2AABEE; }
  .ad-container { margin: 30px 0; text-align: center; }
  .related-section-container { padding: 40px 0; background-color: #181818; }
  .related-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 20px 15px; padding: 0 50px; }
  .movie-card { display: block; cursor: pointer; transition: transform 0.3s ease; }
  .poster-wrapper { position: relative; width: 100%; border-radius: 6px; overflow: hidden; background-color: #222; display: flex; flex-direction: column; }
  .movie-poster-container { position: relative; overflow: hidden; width:100%; flex-grow:1; aspect-ratio: 2 / 3; }
  .movie-poster { width: 100%; height: 100%; object-fit: cover; display: block; transition: transform 0.4s ease; }
  @keyframes rgb-glow { 0%, 100% { color: #ff5555; text-shadow: 0 0 5px #ff5555, 0 0 10px #ff5555; } 33% { color: #55ff55; text-shadow: 0 0 5px #55ff55, 0 0 10px #55ff55; } 66% { color: #55aaff; text-shadow: 0 0 5px #55aaff, 0 0 10px #55aaff; } }
  .poster-badge { position: absolute; top: 18px; left: -35px; width: 140px; background: rgba(20, 20, 20, 0.8); backdrop-filter: blur(5px); transform: rotate(-45deg); text-align: center; z-index: 5; font-size: 0.75rem; font-weight: 700; padding: 4px 0; border: 1px solid rgba(255, 255, 255, 0.2); animation: rgb-glow 3s linear infinite; }
  .rating-badge { position: absolute; top: 10px; right: 10px; background-color: rgba(0, 0, 0, 0.8); color: white; padding: 5px 10px; font-size: 0.8rem; font-weight: 700; border-radius: 20px; z-index: 3; display: flex; align-items: center; gap: 5px; backdrop-filter: blur(5px); }
  .rating-badge .fa-star { color: #f5c518; }
  .card-info-static { padding: 10px 8px; background-color: #1a1a1a; text-align: left; width: 100%; flex-shrink: 0; }
  .card-info-title { font-size: 0.9rem; font-weight: 500; color: var(--text-light); margin: 0 0 4px 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .card-info-meta { font-size: 0.75rem; color: var(--text-dark); margin: 0; }
  @media (hover: hover) { .movie-card:hover { transform: scale(1.05); z-index: 10; box-shadow: 0 0 20px rgba(229, 9, 20, 0.5); } .movie-card:hover .movie-poster { transform: scale(1.1); } }
  @media (max-width: 992px) { .detail-content-wrapper { flex-direction: column; align-items: center; text-align: center; } .detail-info { max-width: 100%; } .detail-title { font-size: 3.5rem; } }
  @media (max-width: 768px) { .detail-header { padding: 20px; } .detail-hero { padding: 80px 20px 40px; } .detail-poster { width: 60%; max-width: 220px; height: auto; } .detail-title { font-size: 2.2rem; }
  .action-buttons-container { flex-direction: column; }
  .episode-item { flex-direction: column; align-items: flex-start; gap: 10px; } .episode-buttons { width: 100%; justify-content: space-between; } .episode-button { flex-grow: 1; justify-content: center; }
  .section-title { margin-left: 15px !important; } .related-section-container { padding: 20px 0; }
  .related-grid { grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 15px 10px; padding: 0 15px; } }
</style>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.2.0/css/all.min.css">
</head>
<body>
{% macro render_movie_card(m) %}
  <a href="{{ url_for('movie_detail', movie_id=m._id) }}" class="movie-card">
    <div class="poster-wrapper">
      <div class="movie-poster-container">
        <img class="movie-poster" loading="lazy" src="{{ m.poster or 'https://via.placeholder.com/400x600.png?text=No+Image' }}" alt="{{ m.title }}">
        {% if m.poster_badge %}<div class="poster-badge">{{ m.poster_badge }}</div>{% endif %}
        {% if m.vote_average and m.vote_average > 0 %}<div class="rating-badge"><i class="fas fa-star"></i> {{ "%.1f"|format(m.vote_average) }}</div>{% endif %}
      </div>
      <div class="card-info-static">
        <h4 class="card-info-title">{{ m.title }}</h4>
        {% if m.release_date %}<p class="card-info-meta">{{ m.release_date.split('-')[0] }}</p>{% endif %}
      </div>
    </div>
  </a>
{% endmacro %}
<header class="detail-header"><a href="{{ url_for('home') }}" class="back-button"><i class="fas fa-arrow-left"></i> Back to Home</a></header>
{% if movie %}
<div class="detail-hero" style="min-height: auto; padding-bottom: 60px;">
  <div class="detail-hero-background" style="background-image: url('{{ movie.poster }}');"></div>
  <div class="detail-content-wrapper"><img class="detail-poster" src="{{ movie.poster or 'https://via.placeholder.com/400x600.png?text=No+Image' }}" alt="{{ movie.title }}">
    <div class="detail-info">
      <h1 class="detail-title">{{ movie.title }}</h1>
      <div class="detail-meta">
        {% if movie.release_date %}<span>{{ movie.release_date.split('-')[0] }}</span>{% endif %}
        {% if movie.vote_average %}<span><i class="fas fa-star" style="color:#f5c518;"></i> {{ "%.1f"|format(movie.vote_average) }}</span>{% endif %}
        {% if movie.view_count %}<span><i class="fas fa-eye" style="color:var(--text-dark);"></i> {{ "{:,}".format(movie.view_count | int) }} Views</span>{% endif %}
        {% if movie.languages %}<span><i class="fas fa-language"></i> {{ movie.languages | join(' • ') }}</span>{% endif %}
        {% if movie.genres %}<span>{{ movie.genres | join(' • ') }}</span>{% endif %}
      </div>
      <p class="detail-overview">{{ movie.overview }}</p>
      
      {% if movie.type == 'movie' and (movie.watch_links or movie.download_links) %}
      <div class="action-buttons-container">
          {% for link in movie.watch_links %}
              {% if link.get('url') %}<a href="{{ link.url }}" target="_blank" rel="noopener" class="action-btn">
                  <i class="fas fa-play"></i> Watch ({{ link.lang }})
              </a>
              {% elif link.get('message_id') %}<a href="https://t.me/{{ bot_username }}?start={{ movie._id }}_Link" class="action-btn telegram">
                  <i class="fa-brands fa-telegram"></i> Watch (Telegram)
              </a>{% endif %}
          {% endfor %}
          {% for link in movie.download_links %}
              {% if link.get('url') %}<a href="{{ link.url }}" target="_blank" rel="noopener" class="action-btn download">
                  <i class="fas fa-download"></i> Download ({{ link.lang }})
              </a>
              {% elif link.get('message_id') %}<a href="https://t.me/{{ bot_username }}?start={{ movie._id }}_Link" class="action-btn download telegram">
                  <i class="fa-brands fa-telegram"></i> Download (Telegram)
              </a>{% endif %}
          {% endfor %}
      </div>
      {% endif %}

      {% if ad_settings.banner_ad_code %}<div class="ad-container">{{ ad_settings.banner_ad_code|safe }}</div>{% endif %}
      {% if trailer_key %}<div class="trailer-section"><h3 class="section-title">Watch Trailer</h3><div class="video-container"><iframe src="https://www.youtube.com/embed/{{ trailer_key }}" frameborder="0" allowfullscreen></iframe></div></div>{% endif %}
      <div style="margin: 20px 0;"><a href="{{ url_for('contact', report_id=movie._id, title=movie.title) }}" class="download-button" style="background-color:#5a5a5a; text-align:center;"><i class="fas fa-flag"></i> Report a Problem</a></div>
      
      {% if movie.is_coming_soon %}<h3 class="section-title">Coming Soon</h3>
      {% elif movie.type == 'movie' %}
        {% if movie.files %}<div class="download-section"><h3 class="section-title">Get from Telegram</h3>{% for file in movie.files | sort(attribute='quality') %}<a href="https://t.me/{{ bot_username }}?start={{ movie._id }}_{{ file.quality }}" class="action-btn" style="background-color: #2AABEE; display: block; text-align:center; margin-top:10px; margin-bottom: 0;"><i class="fa-brands fa-telegram"></i> Get {{ file.quality }}</a>{% endfor %}</div>{% endif %}
      {% elif movie.type == 'series' %}
        <div class="episode-section">
          <h3 class="section-title">Episodes & Seasons</h3>
          {% if movie.season_packs %}
            {% for pack in movie.season_packs | sort(attribute='season') %}
            <div class="episode-item" style="background-color: #3e1a1a; flex-direction: column; align-items: flex-start; gap: 15px;">
                <span class="episode-title">Complete Season {{ pack.season }} Pack</span>
                <div class="episode-buttons" style="width: 100%;">
                    {% for link in pack.watch_links %}
                    {% if link.get('url') %}<a href="{{ link.url }}" target="_blank" class="episode-button" style="flex-grow:1; justify-content:center;"><i class="fas fa-play"></i> Watch ({{link.lang}})</a>
                    {% elif link.get('message_id') %}<a href="https://t.me/{{ bot_username }}?start={{ movie._id }}_S{{ pack.season }}" class="episode-button telegram" style="flex-grow:1; justify-content:center;"><i class="fa-brands fa-telegram"></i> Watch (Telegram)</a>{% endif %}
                    {% endfor %}
                    {% for link in pack.download_links %}
                    {% if link.get('url') %}<a href="{{ link.url }}" target="_blank" class="episode-button download" style="flex-grow:1; justify-content:center;"><i class="fas fa-download"></i> Download ({{link.lang}})</a>
                    {% elif link.get('message_id') %}<a href="https://t.me/{{ bot_username }}?start={{ movie._id }}_S{{ pack.season }}" class="episode-button download telegram" style="flex-grow:1; justify-content:center;"><i class="fa-brands fa-telegram"></i> Download (Telegram)</a>{% endif %}
                    {% endfor %}
                    {% if pack.message_id %}
                    <a href="https://t.me/{{ bot_username }}?start={{ movie._id }}_S{{ pack.season }}" class="episode-button telegram" style="flex-grow:1; justify-content:center;"><i class="fa-brands fa-telegram"></i> Get Pack (Telegram)</a>
                    {% endif %}
                </div>
            </div>
            {% endfor %}
          {% endif %}
          {% if movie.episodes %}
            {% for ep in movie.episodes | sort(attribute='episode_number') | sort(attribute='season') %}
              <div class="episode-item">
                <span class="episode-title">S{{ "%02d"|format(ep.season) }}E{{ "%02d"|format(ep.episode_number) }}: {{ ep.title or 'Episode ' + ep.episode_number|string }}</span>
                <div class="episode-buttons">
                    {% for link in ep.watch_links %}
                      {% if link.get('url') %}<a href="{{ link.url }}" target="_blank" class="episode-button"><i class="fas fa-play"></i> Watch ({{link.lang}})</a>
                      {% elif link.get('message_id') %}<a href="https://t.me/{{ bot_username }}?start={{ movie._id }}_{{ ep.season }}_{{ ep.episode_number }}" class="episode-button telegram"><i class="fa-brands fa-telegram"></i> Watch (Telegram)</a>{% endif %}
                    {% endfor %}
                    {% for link in ep.download_links %}
                      {% if link.get('url') %}<a href="{{ link.url }}" target="_blank" class="episode-button download"><i class="fas fa-download"></i> Download ({{link.lang}})</a>
                      {% elif link.get('message_id') %}<a href="https://t.me/{{ bot_username }}?start={{ movie._id }}_{{ ep.season }}_{{ ep.episode_number }}" class="episode-button download telegram"><i class="fa-brands fa-telegram"></i> Download (Telegram)</a>{% endif %}
                    {% endfor %}
                    {% if ep.message_id %}
                      <a href="https://t.me/{{ bot_username }}?start={{ movie._id }}_{{ ep.season }}_{{ ep.episode_number }}" class="episode-button telegram"><i class="fa-brands fa-telegram"></i> Get (Telegram)</a>
                    {% endif %}
                </div>
              </div>
            {% endfor %}
          {% endif %}
          {% if not movie.episodes and not movie.season_packs %}<p>No episodes or season packs available yet.</p>{% endif %}
        </div>
      {% endif %}
    </div>
  </div>
</div>
{% if related_movies %}<div class="related-section-container"><h3 class="section-title" style="margin-left: 50px; color: white;">You Might Also Like</h3><div class="related-grid">{% for m in related_movies %}{{ render_movie_card(m) }}{% endfor %}</div></div>{% endif %}
{% else %}<div style="display:flex; justify-content:center; align-items:center; height:100vh;"><h2>Content not found.</h2></div>{% endif %}
<script>
function copyToClipboard(text) { navigator.clipboard.writeText(text).then(() => alert('Link copied!'), () => alert('Copy failed!')); }
</script>
{% if ad_settings.popunder_code %}{{ ad_settings.popunder_code|safe }}{% endif %}
{% if ad_settings.social_bar_code %}{{ ad_settings.social_bar_code|safe }}{% endif %}
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
.edit-btn { background: #007bff; } .delete-btn { background: #dc3545; }
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
    <div class="form-group"><label>Poster URL:</label><input type="url" name="poster" /></div>
    <div class="form-group"><label>Overview:</label><textarea name="overview"></textarea></div>
    <div class="form-group"><label>Genres (comma separated):</label><input type="text" name="genres" placeholder="e.g. Action, Drama" /></div>
    <div class="form-group"><label>Languages (comma separated):</label><input type="text" name="languages" placeholder="e.g. Hindi, English, Bangla" /></div>
    <div class="form-group"><label>Poster Badge:</label><input type="text" name="poster_badge" placeholder="e.g. DUB, NEW, UHD" /></div>
    <div class="form-group"><label>Content Type:</label><select name="content_type" id="content_type" onchange="toggleFields()"><option value="movie">Movie</option><option value="series">TV/Web Series</option></select></div>
    
    <div id="movie_fields">
      <div class="form-group"><label>Watch Links (e.g., Hindi: url1, Bangla: url2, or Telegram: MsgId:123):</label><textarea name="watch_links_str"></textarea></div>
      <div class="form-group"><label>Download Links (e.g., 480p: url1, 720p: url2, or Telegram: MsgId:456):</label><textarea name="download_links_str"></textarea></div>
      <hr><p><b>OR</b> Get Movie from Telegram (Old Method)</p>
      <div id="telegram_files_container"></div><button type="button" onclick="addTelegramFileField()" class="add-btn">Add Telegram File</button>
    </div>

    <div id="episode_fields" style="display: none;">
      <h3>Season Packs</h3>
      <div id="season_packs_container"></div>
      <button type="button" onclick="addSeasonPackField()" class="add-btn">Add Season Pack</button>
      <hr style="margin: 20px 0;">

      <h3>Individual Episodes</h3>
      <div id="episodes_container"></div>
      <button type="button" onclick="addEpisodeField()" class="add-btn">Add Episode</button>
    </div>
    
    <hr style="margin: 20px 0;">
    <div class="form-group"><input type="checkbox" name="is_trending" value="true"><label style="display: inline-block;">Is Trending?</label></div>
    <div class="form-group"><input type="checkbox" name="is_coming_soon" value="true"><label style="display: inline-block;">Is Coming Soon?</label></div>
    <button type="submit">Add Content</button>
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
    <tr><td>{{ movie.title }}</td><td>{{ movie.type | title }}</td><td class="action-buttons"><a href="{{ url_for('edit_movie', movie_id=movie._id) }}" class="edit-btn">Edit</a><button class="delete-btn" onclick="confirmDelete('{{ movie._id }}', '{{ movie.title }}')">Delete</button></td></tr>
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
    function addEpisodeField() { const c = document.getElementById('episodes_container'); const d = document.createElement('div'); d.className = 'dynamic-item'; d.innerHTML = `<div class="form-group"><label>Season Number:</label><input type="number" name="episode_season[]" value="1" required /></div><div class="form-group"><label>Episode Number:</label><input type="number" name="episode_number[]" required /></div><div class="form-group"><label>Episode Title:</label><input type="text" name="episode_title[]" /></div><div class="form-group"><label>Watch Links (e.g., Hindi: url, Eng: url, or Telegram: MsgId:123):</label><textarea name="episode_watch_links_str[]"></textarea></div><div class="form-group"><label>Download Links (e.g., 480p: url, 720p: url, or Telegram: MsgId:456):</label><textarea name="episode_download_links_str[]"></textarea></div><button type="button" onclick="this.parentElement.remove()" class="delete-btn">Remove Episode</button>`; c.appendChild(d); }
    function addSeasonPackField() { const c = document.getElementById('season_packs_container'); const d = document.createElement('div'); d.className = 'dynamic-item'; d.innerHTML = `<div class="form-group"><label>Season Number:</label><input type="number" name="pack_season[]" required /></div><div class="form-group"><label>Watch Links (e.g., Hindi: url, Eng: url, or Telegram: MsgId:123):</label><textarea name="pack_watch_links_str[]"></textarea></div><div class="form-group"><label>Download Links (e.g., 480p: url, 720p: url, or Telegram: MsgId:456):</label><textarea name="pack_download_links_str[]"></textarea></div><button type="button" onclick="this.parentElement.remove()" class="delete-btn">Remove Pack</button>`; c.appendChild(d); }
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
        <div class="form-group"><label>Watch Links (e.g., Hindi: url1, Bangla: url2, or Telegram: MsgId:123):</label><textarea name="watch_links_str">{{ format_links_for_edit(movie.watch_links) }}</textarea></div>
        <div class="form-group"><label>Download Links (e.g., 480p: url1, 720p: url2, or Telegram: MsgId:456):</label><textarea name="download_links_str">{{ format_links_for_edit(movie.download_links) }}</textarea></div>
        <hr><p><b>OR</b> Get from Telegram (Old Method)</p>
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
          {% if movie.type == 'series' and movie.season_packs %}
              {% for pack in movie.season_packs | sort(attribute='season') %}
              <div class="dynamic-item">
                <div class="form-group"><label>Season Number:</label><input type="number" name="pack_season[]" value="{{ pack.season }}" required /></div>
                <div class="form-group"><label>Watch Links (e.g., Hindi: url, Eng: url, or Telegram: MsgId:123):</label><textarea name="pack_watch_links_str[]">{{ format_links_for_edit(pack.watch_links) }}</textarea></div>
                <div class="form-group"><label>Download Links (e.g., 480p: url, 720p: url, or Telegram: MsgId:456):</label><textarea name="pack_download_links_str[]">{{ format_links_for_edit(pack.download_links) }}</textarea></div>
                <button type="button" onclick="this.parentElement.remove()" class="delete-btn">Remove Pack</button>
              </div>
              {% endfor %}
          {% endif %}
      </div>
      <button type="button" onclick="addSeasonPackField()" class="add-btn">Add Season Pack</button>
      <hr style="margin: 20px 0;">

      <h3>Individual Episodes</h3>
      <div id="episodes_container">
      {% if movie.type == 'series' and movie.episodes %}{% for ep in movie.episodes | sort(attribute='episode_number') | sort(attribute='season') %}<div class="dynamic-item">
        <div class="form-group"><label>Season Number:</label><input type="number" name="episode_season[]" value="{{ ep.season or 1 }}" required /></div>
        <div class="form-group"><label>Ep Number:</label><input type="number" name="episode_number[]" value="{{ ep.episode_number }}" required /></div>
        <div class="form-group"><label>Ep Title:</label><input type="text" name="episode_title[]" value="{{ ep.title or '' }}" /></div>
        <div class="form-group"><label>Watch Links (e.g., Hindi: url, Eng: url, or Telegram: MsgId:123):</label><textarea name="episode_watch_links_str[]">{{ format_links_for_edit(ep.watch_links) }}</textarea></div>
        <div class="form-group"><label>Download Links (e.g., 480p: url, 720p: url, or Telegram: MsgId:456):</label><textarea name="episode_download_links_str[]">{{ format_links_for_edit(ep.download_links) }}</textarea></div>
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
    function addEpisodeField() { const c = document.getElementById('episodes_container'); const d = document.createElement('div'); d.className = 'dynamic-item'; d.innerHTML = `<div class="form-group"><label>Season Number:</label><input type="number" name="episode_season[]" value="1" required /></div><div class="form-group"><label>Episode Number:</label><input type="number" name="episode_number[]" required /></div><div class="form-group"><label>Episode Title:</label><input type="text" name="episode_title[]" /></div><div class="form-group"><label>Watch Links (e.g., Hindi: url, Eng: url, or Telegram: MsgId:123):</label><textarea name="episode_watch_links_str[]"></textarea></div><div class="form-group"><label>Download Links (e.g., 480p: url, 720p: url, or Telegram: MsgId:456):</label><textarea name="episode_download_links_str[]"></textarea></div><button type="button" onclick="this.parentElement.remove()" class="delete-btn">Remove Episode</button>`; c.appendChild(d); }
    function addSeasonPackField() { const c = document.getElementById('season_packs_container'); const d = document.createElement('div'); d.className = 'dynamic-item'; d.innerHTML = `<div class="form-group"><label>Season Number:</label><input type="number" name="pack_season[]" required /></div><div class="form-group"><label>Watch Links (e.g., Hindi: url, Eng: url, or Telegram: MsgId:123):</label><textarea name="pack_watch_links_str[]"></textarea></div><div class="form-group"><label>Download Links (e.g., 480p: url, 720p: url, or Telegram: MsgId:456):</label><textarea name="pack_download_links_str[]"></textarea></div><button type="button" onclick="this.parentElement.remove()" class="delete-btn">Remove Pack</button>`; c.appendChild(d); }
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
def get_tmdb_details_from_api(title_for_search, content_type, year=None):
    if not TMDB_API_KEY:
        print("ERROR: TMDB_API_KEY is not set.")
        return None
    
    search_type = "tv" if content_type in ["series", "series_pack"] else "movie"
    
    def search_tmdb(query_title, query_year):
        print(f"INFO: Searching TMDb for: '{query_title}' (Type: {search_type}, Year: {query_year})")
        try:
            search_url = f"https://api.themoviedb.org/3/search/{search_type}?api_key={TMDB_API_KEY}&query={requests.utils.quote(query_title)}&language=en-US"
            if query_year and search_type == "movie":
                search_url += f"&year={query_year}"
            elif query_year and search_type == "tv":
                search_url += f"&first_air_date_year={query_year}"

            search_res = requests.get(search_url, timeout=10)
            search_res.raise_for_status()
            results = search_res.json().get("results")
            
            if not results: return None
            
            # Prioritize exact title match or closest year match if multiple results
            best_match = None
            if query_year:
                for r in results:
                    release_year = (r.get('release_date') or r.get('first_air_date', ''))[:4]
                    if release_year == str(query_year):
                        best_match = r
                        break
            if not best_match:
                best_match = results[0] # Fallback to first result
            
            tmdb_id = best_match.get("id")
            detail_url = f"https://api.themoviedb.org/3/{search_type}/{tmdb_id}?api_key={TMDB_API_KEY}&language=en-US&append_to_response=videos"
            detail_res = requests.get(detail_url, timeout=10)
            detail_res.raise_for_status()
            res_json = detail_res.json()
            
            trailer_key = next((v['key'] for v in res_json.get("videos", {}).get("results", []) if v.get('type') == 'Trailer' and v.get('site') == 'YouTube'), None)
            
            # Filter out 'English' from TMDb languages unless explicitly needed
            language_names = [lang['english_name'] for lang in res_json.get('spoken_languages', []) if lang['english_name'].lower() != 'english']

            details = {
                "tmdb_id": tmdb_id, 
                "tmdb_title": res_json.get("title") or res_json.get("name"),
                "poster": f"https://image.tmdb.org/t/p/w500{res_json.get('poster_path')}" if res_json.get('poster_path') else None, 
                "overview": res_json.get("overview"), 
                "release_date": res_json.get("release_date") or res_json.get("first_air_date"), 
                "genres": [g['name'] for g in res_json.get("genres", [])], 
                "languages": language_names,
                "vote_average": res_json.get("vote_average"), 
                "trailer_key": trailer_key
            }
            print(f"SUCCESS: Found TMDb details for '{query_title}' (ID: {tmdb_id}).")
            return details
        except requests.RequestException as e:
            print(f"ERROR: TMDb API request failed for '{query_title}'. Reason: {e}")
            return None

    tmdb_data = search_tmdb(title_for_search, year)
    if not tmdb_data and year:
        print(f"WARNING: TMDb search failed for '{title_for_search}' with year '{year}'. Retrying without year.")
        tmdb_data = search_tmdb(title_for_search, None)
        
    if not tmdb_data:
        print(f"FINAL WARNING: TMDb search found no results for '{title_for_search}' after all attempts.")
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
    all_badges = sorted([badge for badge in movies.distinct("poster_badge") if badge and badge.strip()])
    limit = 12
    context = {
        "trending_movies": process_movie_list(list(movies.find({"is_trending": True, "is_coming_soon": {"$ne": True}}).sort('_id', -1).limit(limit))),
        "latest_movies": process_movie_list(list(movies.find({"type": "movie", "is_coming_soon": {"$ne": True}}).sort('_id', -1).limit(limit))),
        "latest_series": process_movie_list(list(movies.find({"type": "series", "is_coming_soon": {"$ne": True}}).sort('_id', -1).limit(limit))),
        "coming_soon_movies": process_movie_list(list(movies.find({"is_coming_soon": True}).sort('_id', -1).limit(limit))),
        "recently_added": process_movie_list(list(movies.find({"is_coming_soon": {"$ne": True}}).sort('_id', -1).limit(6))),
        "recently_added_full": process_movie_list(list(movies.find({"is_coming_soon": {"$ne": True}}).sort('_id', -1).limit(limit))),
        "is_full_page_list": False, "query": "", "all_badges": all_badges
    }
    return render_template_string(index_html, **context)

@app.route('/movie/<movie_id>')
def movie_detail(movie_id):
    try:
        obj_id = ObjectId(movie_id)
        movies.update_one({"_id": obj_id}, {"$inc": {"view_count": 1}})
        movie = movies.find_one({"_id": obj_id})
        if not movie: return "Content not found", 404
        related_movies = []
        if movie.get("genres"):
            related_movies = list(movies.find({"genres": {"$in": movie["genres"]}, "_id": {"$ne": obj_id}}).limit(12))
        return render_template_string(detail_html, movie=movie, trailer_key=movie.get("trailer_key"), related_movies=process_movie_list(related_movies))
    except Exception as e:
        print(f"Error in movie_detail route: {e}")
        return "Content not found or invalid ID", 404

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
# --- Admin and Other Routes ---
# ======================================================================
@app.route('/admin', methods=["GET", "POST"])
@requires_auth
def admin():
    if request.method == "POST":
        user_title = request.form.get("title")
        content_type = request.form.get("content_type", "movie")
        
        # Manually provided details
        manual_poster = request.form.get("poster", "").strip()
        manual_overview = request.form.get("overview", "").strip()
        manual_genres = [g.strip() for g in request.form.get("genres", "").split(',') if g.strip()]
        manual_languages = [lang.strip() for lang in request.form.get("languages", "").split(',') if lang.strip()]
        manual_poster_badge = request.form.get("poster_badge", "").strip() or None

        tmdb_data = get_tmdb_details_from_api(user_title, content_type) or {}
        
        doc_data = {
            "title": user_title, 
            "type": content_type,
            "is_trending": request.form.get("is_trending") == "true", 
            "is_coming_soon": request.form.get("is_coming_soon") == "true", 
            "view_count": 0,
            "created_at": datetime.now(timezone.utc)
        }
        
        # Merge TMDb data with manual overrides
        doc_data['poster'] = manual_poster if manual_poster else tmdb_data.get('poster')
        doc_data['overview'] = manual_overview if manual_overview else tmdb_data.get('overview')
        doc_data['genres'] = manual_genres if manual_genres else tmdb_data.get('genres', [])
        doc_data['languages'] = manual_languages if manual_languages else tmdb_data.get('languages', [])
        doc_data['poster_badge'] = manual_poster_badge if manual_poster_badge else tmdb_data.get('poster_badge') # poster_badge usually manually set
        doc_data['release_date'] = tmdb_data.get('release_date')
        doc_data['vote_average'] = tmdb_data.get('vote_average')
        doc_data['trailer_key'] = tmdb_data.get('trailer_key')
        doc_data['tmdb_id'] = tmdb_data.get('tmdb_id')

        if content_type == "movie":
            doc_data['watch_links'] = parse_links_from_string(request.form.get('watch_links_str', ''))
            doc_data['download_links'] = parse_links_from_string(request.form.get('download_links_str', ''))
            doc_data['files'] = [{"quality": q, "message_id": int(mid)} for q, mid in zip(request.form.getlist('telegram_quality[]'), request.form.getlist('telegram_message_id[]')) if q and mid]
            doc_data["episodes"] = [] # Ensure these are empty for movies
            doc_data["season_packs"] = []
        else: # Series
            doc_data["episodes"] = []
            for i in range(len(request.form.getlist('episode_season[]'))):
                ep_data = {
                    "season": int(request.form.getlist('episode_season[]')[i]),
                    "episode_number": int(request.form.getlist('episode_number[]')[i]),
                    "title": request.form.getlist('episode_title[]')[i].strip() or f"Episode {request.form.getlist('episode_number[]')[i]}",
                    "watch_links": parse_links_from_string(request.form.getlist('episode_watch_links_str[]')[i]),
                    "download_links": parse_links_from_string(request.form.getlist('episode_download_links_str[]')[i]),
                }
                doc_data["episodes"].append(ep_data)

            doc_data["season_packs"] = []
            for i in range(len(request.form.getlist('pack_season[]'))):
                pack_data = {
                    "season": int(request.form.getlist('pack_season[]')[i]),
                    "watch_links": parse_links_from_string(request.form.getlist('pack_watch_links_str[]')[i]),
                    "download_links": parse_links_from_string(request.form.getlist('pack_download_links_str[]')[i]),
                }
                doc_data["season_packs"].append(pack_data)

            doc_data["watch_links"] = [] # Ensure these are empty for series
            doc_data["download_links"] = []
            doc_data["files"] = []
        
        # Try to find existing content by TMDb ID or title to avoid duplicates
        existing_movie = None
        if tmdb_data and tmdb_data.get('tmdb_id'):
            existing_movie = movies.find_one({"tmdb_id": tmdb_data['tmdb_id']})
        if not existing_movie:
            existing_movie = movies.find_one({"title": user_title, "type": content_type})

        if existing_movie:
            movies.update_one({"_id": existing_movie['_id']}, {"$set": doc_data})
            content_id_to_post = existing_movie['_id']
            print(f"INFO: Updated existing content: {user_title}")
        else:
            result = movies.insert_one(doc_data)
            content_id_to_post = result.inserted_id
            print(f"INFO: Added new content: {user_title}")
        
        if content_id_to_post:
            post_to_public_channel(content_id_to_post, post_type='content')

        return redirect(url_for('admin'))

    search_query = request.args.get('search', '').strip()
    query_filter = {}
    if search_query: query_filter = {"title": {"$regex": search_query, "$options": "i"}}
    ad_settings = settings.find_one() or {}
    content_list = process_movie_list(list(movies.find(query_filter).sort('_id', -1)))
    feedback_list = process_movie_list(list(feedback.find().sort('timestamp', -1)))
    return render_template_string(admin_html, content_list=content_list, feedback_list=feedback_list, search_query=search_query)


@app.route('/admin/save_ads', methods=['POST'])
@requires_auth
def save_ads():
    ad_codes = { "popunder_code": request.form.get("popunder_code", ""), "social_bar_code": request.form.get("social_bar_code", ""), "banner_ad_code": request.form.get("banner_ad_code", ""), "native_banner_code": request.form.get("native_banner_code", "") }
    settings.update_one({}, {"$set": ad_codes}, upsert=True)
    return redirect(url_for('admin'))

@app.route('/edit_movie/<movie_id>', methods=["GET", "POST"])
@requires_auth
def edit_movie(movie_id):
    obj_id = ObjectId(movie_id)
    movie_obj = movies.find_one({"_id": obj_id})
    if not movie_obj: return "Movie not found", 404

    if request.method == "POST":
        content_type = request.form.get("content_type", "movie")
        update_data = {
            "title": request.form.get("title"), "type": content_type,
            "is_trending": request.form.get("is_trending") == "true", "is_coming_soon": request.form.get("is_coming_soon") == "true",
            "poster": request.form.get("poster", "").strip(), "overview": request.form.get("overview", "").strip(),
            "genres": [g.strip() for g in request.form.get("genres", "").split(',') if g.strip()],
            "languages": [lang.strip() for lang in request.form.get("languages", "").split(',') if lang.strip()],
            "poster_badge": request.form.get("poster_badge", "").strip() or None
        }
        
        if content_type == "movie":
            update_data["watch_links"] = parse_links_from_string(request.form.get('watch_links_str'))
            update_data["download_links"] = parse_links_from_string(request.form.get('download_links_str'))
            update_data["files"] = [{"quality": q, "message_id": int(mid)} for q, mid in zip(request.form.getlist('telegram_quality[]'), request.form.getlist('telegram_message_id[]')) if q and mid]
            movies.update_one({"_id": obj_id}, {"$set": update_data, "$unset": {"episodes": "", "season_packs": ""}})
        else: # Series
            new_episodes = []
            for i in range(len(request.form.getlist('episode_season[]'))):
                ep_data = {
                    "season": int(request.form.getlist('episode_season[]')[i]),
                    "episode_number": int(request.form.getlist('episode_number[]')[i]),
                    "title": request.form.getlist('episode_title[]')[i].strip() or f"Episode {request.form.getlist('episode_number[]')[i]}",
                    "watch_links": parse_links_from_string(request.form.getlist('episode_watch_links_str[]')[i]),
                    "download_links": parse_links_from_string(request.form.getlist('episode_download_links_str[]')[i]),
                }
                new_episodes.append(ep_data)

            new_season_packs = []
            for i in range(len(request.form.getlist('pack_season[]'))):
                pack_data = {
                    "season": int(request.form.getlist('pack_season[]')[i]),
                    "watch_links": parse_links_from_string(request.form.getlist('pack_watch_links_str[]')[i]),
                    "download_links": parse_links_from_string(request.form.getlist('pack_download_links_str[]')[i]),
                }
                new_season_packs.append(pack_data)

            update_data["episodes"] = new_episodes
            update_data["season_packs"] = new_season_packs
            
            movies.update_one({"_id": obj_id}, {"$set": update_data, "$unset": {"watch_links": "", "download_links": "", "files": ""}})
        
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
    movies.delete_many({})
    return redirect(url_for('admin'))

@app.route('/contact', methods=['GET', 'POST'])
def contact():
    if request.method == 'POST':
        feedback_data = {
            "type": request.form.get("type"), 
            "content_title": request.form.get("content_title"), 
            "message": request.form.get("message"), 
            "email": request.form.get("email", "").strip(), 
            "reported_content_id": request.form.get("reported_content_id"), 
            "timestamp": datetime.now(timezone.utc)
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


# ======================================================================
# --- Webhook Route (FINAL VERSION) ---
# ======================================================================
@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    data = request.get_json()

    if 'channel_post' in data:
        pass

    elif 'message' in data:
        message = data['message']
        chat_id = message['chat']['id']
        text = message.get('text', '').strip()
        
        # --- /start command handling ---
        if text.startswith('/start'):
            payload_str = text.split(' ', 1)[-1]
            if payload_str != '/start':
                try:
                    parts = payload_str.split('_')
                    movie_id_str = parts[0]
                    movie = movies.find_one({"_id": ObjectId(movie_id_str)})
                    if not movie: raise ValueError("Movie not found")

                    # Handle watch/download links that reference a message_id
                    if len(parts) == 2 and parts[1] == 'Link':
                        # For movies with Telegram message_id in watch/download links
                        # This scenario needs more specific handling if multiple links refer to different message_ids
                        # For simplicity, if any watch_link or download_link has a message_id, send it.
                        # You might need to refine this based on how you store/want to retrieve specific links.
                        found_message_id = None
                        for link_list in [movie.get('watch_links', []), movie.get('download_links', [])]:
                            for link in link_list:
                                if link.get('message_id'):
                                    found_message_id = link['message_id']
                                    break
                            if found_message_id: break
                        
                        if found_message_id:
                            requests.post(f"{TELEGRAM_API_URL}/copyMessage", json={'chat_id': chat_id, 'from_chat_id': ADMIN_CHANNEL_ID, 'message_id': found_message_id})
                        else:
                            requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': "Sorry, this specific link is not available via Telegram or message ID not found. Please check the website for direct links."})

                    elif len(parts) == 2 and parts[1].startswith('S'): # Season pack
                        season_num = int(parts[1][1:])
                        pack = next((p for p in movie.get('season_packs', []) if p['season'] == season_num), None)
                        if pack and pack.get('message_id'):
                            requests.post(f"{TELEGRAM_API_URL}/copyMessage", json={'chat_id': chat_id, 'from_chat_id': ADMIN_CHANNEL_ID, 'message_id': pack['message_id']})
                        else:
                            requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': "Sorry, this season pack is not available via Telegram. Please check the website for direct links."})
                    
                    elif len(parts) == 2: # Movie file (old method for direct files)
                        quality = parts[1]
                        file_info = next((f for f in movie.get('files', []) if f['quality'] == quality), None)
                        if file_info:
                            requests.post(f"{TELEGRAM_API_URL}/copyMessage", json={'chat_id': chat_id, 'from_chat_id': ADMIN_CHANNEL_ID, 'message_id': file_info['message_id']})
                    
                    elif len(parts) == 3: # Series episode
                        season, episode = int(parts[1]), int(parts[2])
                        ep_info = next((e for e in movie.get('episodes', []) if e['season'] == season and e['episode_number'] == episode), None)
                        if ep_info and ep_info.get('message_id'):
                            requests.post(f"{TELEGRAM_API_URL}/copyMessage", json={'chat_id': chat_id, 'from_chat_id': ADMIN_CHANNEL_ID, 'message_id': ep_info['message_id']})
                        else:
                             requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': "Sorry, this episode is not available via Telegram. Please check the website for direct links."})

                except Exception as e:
                    print(f"Error processing start payload: {e}")
                    requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': "দুঃখিত, আপনার অনুরোধটি প্রক্রিয়া করা যায়নি।"})
            else:
                 welcome_text = (f"👋 Welcome!\n\nI am {BOT_USERNAME}, your assistant for finding movies and series.\n\n"
                                 f"🌐 Please visit our website to browse thousands of titles: {WEBSITE_URL}")
                 requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': welcome_text, 'disable_web_page_preview': 'true'})
            
            return jsonify(status='ok')

        # --- Admin commands (only for ADMIN_USER_IDS) ---
        if str(chat_id) not in ADMIN_USER_IDS:
            return jsonify(status='ok')
        
        # --- /add command (for movies, existing logic) ---
        if text.startswith('/add '):
            try:
                parts = text.split('/add ', 1)[1].split('|')
                if len(parts) != 3: raise ValueError("Incorrect format")
                title_part, watch_links_str, download_links_str = [p.strip() for p in parts]
                
                lang_match = re.search(r'\[(.*?)\]', title_part)
                badge = lang_match.group(1).strip() if lang_match else None
                title_part_cleaned = re.sub(r'\s*\[.*?\]', '', title_part).strip()

                year_match = re.search(r'\(?(\d{4})\)?$', title_part_cleaned)
                year, user_title = (year_match.group(1), re.sub(r'\s*\(?\d{4}\)?$', '', title_part_cleaned).strip()) if year_match else (None, title_part_cleaned)

                requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': f"⏳ Searching for `{user_title}`...", 'parse_mode': 'Markdown'})
                tmdb_data = get_tmdb_details_from_api(user_title, "movie", year)
                
                if not tmdb_data:
                    requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': f"❌ Sorry, could not find any movie named '{user_title}'."})
                    return jsonify(status='ok')
                
                final_languages = tmdb_data.get('languages', [])
                # Ensure the user-provided badge language is present
                if badge and badge.title() not in final_languages:
                    final_languages.insert(0, badge.title()) # Add user-defined language at the beginning
                
                movie_doc = {
                    "title": user_title, 
                    "type": "movie",
                    "poster": tmdb_data.get('poster'),
                    "overview": tmdb_data.get('overview'),
                    "release_date": tmdb_data.get('release_date'),
                    "genres": tmdb_data.get('genres', []),
                    "languages": final_languages, # Use filtered and user-augmented languages
                    "vote_average": tmdb_data.get('vote_average'),
                    "trailer_key": tmdb_data.get('trailer_key'),
                    "tmdb_id": tmdb_data.get('tmdb_id'),
                    "poster_badge": badge, 
                    "watch_links": parse_links_from_string(watch_links_str), 
                    "download_links": parse_links_from_string(download_links_str), 
                    "files": [], # This command doesn't handle separate telegram files, use admin panel for that
                    "episodes": [],
                    "season_packs": [],
                    "is_trending": False,
                    "is_coming_soon": False,
                    "view_count": 0,
                    "created_at": datetime.now(timezone.utc)
                }
                
                # Check for existing movie by TMDb ID or title to update instead of creating new
                existing_movie = None
                if movie_doc.get('tmdb_id'):
                    existing_movie = movies.find_one({"tmdb_id": movie_doc['tmdb_id']})
                if not existing_movie:
                    existing_movie = movies.find_one({"title": user_title, "type": "movie"})

                if existing_movie:
                    movies.update_one({"_id": existing_movie['_id']}, {"$set": movie_doc})
                    content_id_to_post = existing_movie['_id']
                    print(f"INFO: Updated existing movie: {user_title}")
                else:
                    result = movies.insert_one(movie_doc)
                    content_id_to_post = result.inserted_id
                    print(f"INFO: Added new movie: {user_title}")

                if content_id_to_post:
                    post_to_public_channel(content_id_to_post, post_type='content')
                
                requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': f"✅ Successfully added/updated `{user_title}` to the website.", 'parse_mode': 'Markdown'})
            except Exception as e:
                print(f"Error in /add command: {e}")
                requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': "❌ Wrong format for /add! Use `/add` for help."})
        
        elif text == '/add':
            reply_text = (f"👇 Use the format below to add a movie:\n\n"
                          f"`/add Movie Name (Year) [Language] | Watch Links | Download Links`\n\n"
                          f"*Separate multiple links with commas. E.g., `Hindi: url, Bangla: url`*\n"
                          f"*For Telegram links: `Telegram: MsgId:12345`*")
            requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': reply_text, 'parse_mode': 'Markdown'})

        # --- New /addseriesfull command (for full series) ---
        elif text.startswith('/addseriesfull '):
            try:
                # Splitting the main parts
                main_parts = text.split('/addseriesfull ', 1)[1].split('|')
                if not (2 <= len(main_parts) <= 3): raise ValueError("Incorrect format for /addseriesfull")

                title_part = main_parts[0].strip()
                season_packs_str = ""
                episodes_str = ""

                # Determine which sections are present
                for part in main_parts[1:]:
                    part = part.strip()
                    if part.lower().startswith('seasonpacks:'):
                        season_packs_str = part.split(':', 1)[1].strip()
                    elif part.lower().startswith('episodes:'):
                        episodes_str = part.split(':', 1)[1].strip()
                    else:
                        raise ValueError("Unknown section in /addseriesfull command.")

                lang_match = re.search(r'\[(.*?)\]', title_part)
                badge = lang_match.group(1).strip() if lang_match else None
                title_part_cleaned = re.sub(r'\s*\[.*?\]', '', title_part).strip()

                year_match = re.search(r'\(?(\d{4})\)?$', title_part_cleaned)
                year, user_title = (year_match.group(1), re.sub(r'\s*\(?\d{4}\)?$', '', title_part_cleaned).strip()) if year_match else (None, title_part_cleaned)

                requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': f"⏳ Searching for series `{user_title}` and processing links...", 'parse_mode': 'Markdown'})
                tmdb_data = get_tmdb_details_from_api(user_title, "series", year)

                if not tmdb_data:
                    requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': f"❌ Sorry, could not find any series named '{user_title}'."})
                    return jsonify(status='ok')
                
                final_languages = tmdb_data.get('languages', [])
                if badge and badge.title() not in final_languages:
                    final_languages.insert(0, badge.title())
                
                # Parse season packs and episodes using helper
                parsed_season_packs = parse_series_links_from_string(season_packs_str, is_pack=True)
                parsed_episodes = parse_series_links_from_string(episodes_str, is_pack=False)

                series_doc = {
                    "title": user_title, 
                    "type": "series",
                    "poster": tmdb_data.get('poster'),
                    "overview": tmdb_data.get('overview'),
                    "release_date": tmdb_data.get('release_date'),
                    "genres": tmdb_data.get('genres', []),
                    "languages": final_languages, 
                    "vote_average": tmdb_data.get('vote_average'),
                    "trailer_key": tmdb_data.get('trailer_key'),
                    "tmdb_id": tmdb_data.get('tmdb_id'),
                    "poster_badge": badge, 
                    "episodes": parsed_episodes, 
                    "season_packs": parsed_season_packs,
                    "watch_links": [], # Series won't have these top-level links
                    "download_links": [],
                    "files": [],
                    "is_trending": False,
                    "is_coming_soon": False,
                    "view_count": 0,
                    "created_at": datetime.now(timezone.utc)
                }
                
                existing_series = None
                if series_doc.get('tmdb_id'):
                    existing_series = movies.find_one({"tmdb_id": series_doc['tmdb_id']})
                if not existing_series:
                    existing_series = movies.find_one({"title": user_title, "type": "series"})

                if existing_series:
                    movies.update_one({"_id": existing_series['_id']}, {"$set": series_doc})
                    content_id_to_post = existing_series['_id']
                    print(f"INFO: Updated existing series: {user_title}")
                else:
                    result = movies.insert_one(series_doc)
                    content_id_to_post = result.inserted_id
                    print(f"INFO: Added new series: {user_title}")

                if content_id_to_post:
                    post_to_public_channel(content_id_to_post, post_type='content')

                requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': f"✅ Successfully added/updated series `{user_title}` with all details.", 'parse_mode': 'Markdown'})
            except Exception as e:
                print(f"Error in /addseriesfull command: {e}")
                requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': "❌ Wrong format! Use `/addseriesfull` for help."})
        
        elif text == '/addseriesfull':
            reply_text = (f"👇 Use the format below to add a series with all details:\n\n"
                          f"`/addseriesfull Series Name (Year) [Language] | SeasonPacks: S1:WatchLink1,DownloadLink1;S2:MsgId:12345 | Episodes: S1E1(Ep Title):WatchLink_E1,DownloadLink_E1;S1E2:MsgId:67890`\n\n"
                          f"*Use `S<SeasonNum>` for season packs, `S<SeasonNum>E<EpisodeNum>(Ep Title)` for episodes.*\n"
                          f"*Separate multiple links with commas. Use `;` to separate multiple season packs/episodes.*\n"
                          f"*For Telegram link, use `MsgId:12345` instead of URLs.*")
            requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': reply_text, 'parse_mode': 'Markdown'})

        # Remove old series commands, as /addseriesfull replaces them
        elif text.startswith('/addseries ') or text == '/addseries':
            requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': "❌ `/addseries` command is deprecated. Please use the comprehensive `/addseriesfull` command or the admin panel."})
        elif text.startswith('/addepisode ') or text == '/addepisode':
            requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': "❌ `/addepisode` command is deprecated. Please use the comprehensive `/addseriesfull` command or the admin panel."})
        elif text.startswith('/addseasonpack ') or text == '/addseasonpack':
            requests.get(f"{TELEGRAM_API_URL}/sendMessage", params={'chat_id': chat_id, 'text': "❌ `/addseasonpack` command is deprecated. Please use the comprehensive `/addseriesfull` command or the admin panel."})

    return jsonify(status='ok')

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

