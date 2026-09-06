from app import app, db
from flask import request, jsonify
from google import genai
from env import EnvConfig
from ..models import *


gemini = genai.Client(**EnvConfig.gemini_client_kwargs())
