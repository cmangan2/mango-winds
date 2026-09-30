[Resource from github at repo://cmangan2/mango-winds/sha/3b97575d021df64e33c42cf52addf291a1fb9edd/contents/mangowinds.py] from flask import Flask, render_template, make_response, jsonify, request, send_from_directory, redirect
import os, json as _json, re, math, threading, time, traceback
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# =====================================================
# 📁 FILE PATHS (override via env vars on Render)
# =====================================================
_forecast_cache = {}
CACHE_TTL = timedelta(minutes=120)  # 2 hour cache to reduce API calls
CACHE_FILE  = os.environ.get("CACHE_FILE", os.path.join(os.path.dirname(__file__), "winds_cache.json"))
USER_DZ_FILE    = os.environ.get("USER_DZ_FILE",    os.path.join(os.path.dirname(CACHE_FILE), "user_dropzones.txt"))
PENDING_DZ_FILE = os.environ.get("PENDING_DZ_FILE", os.path.join(os.path.dirname(CACHE_FILE), "pending_dropzones.txt"))
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
ADMIN_TOKEN     = os.environ.get("ADMIN_TOKEN", "mango-admin-2024")
GITHUB_TOKEN    = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO     = os.environ.get("GITHUB_REPO", "cmangan2/mango-winds")
GH_DZ_FILE      = "Dropzone list.txt"   # path inside the repo
VISITS_FILE   = os.environ.get("VISITS_FILE",   os.path.join(os.path.dirname(__file__), "visits.json"))
JUMPRUN_FILE  = os.environ.get("JUMPRUN_FILE",  os.path.join(os.path.dirname(__file__), "jumprun.json"))
TAILS_FILE    = os.environ.get("TAILS_FILE",    os.path.join(os.path.dirname(__file__), "tails.json"))
LASTLOAD_FILE = os.environ.get("LASTLOAD_FILE", os.path.join(os.path.dirname(__file__), "lastload.json"))