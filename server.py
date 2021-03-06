from gevent import monkey
from gevent.wsgi import WSGIServer
monkey.patch_all()
import os
import requests
from datetime import datetime, timedelta
import redis
import traceback
import math
import time
import json
import jwt
from fuzzywuzzy import process, fuzz
from flask import Flask, Response, request, jsonify, current_app, redirect, url_for, session
from flask_cors import CORS
from flask_oauthlib.client import OAuth
from werkzeug.contrib.fixers import ProxyFix
from werkzeug.security import gen_salt
from pymongo import MongoClient, DESCENDING
from bson import ObjectId
from functools import wraps
from raven.contrib.flask import Sentry

try:
    import xml.etree.cElementTree as ET
except ImportError:
    import xml.etree.ElementTree as ET

# Configuration
etf_host = 'localhost'
redis_host = 'localhost'
redirect_host = os.environ.get('ETF_API_OAUTH_REDIRECT', 'http://localhost:3000')

standard_headers = {'user_agent': 'https://eve.exchange'}

mongo_client = MongoClient()

mongo_db = mongo_client.eveexchange

settings_collection = mongo_db.settings
portfolio_collection = mongo_db.portfolios
subscription_collection = mongo_db.subscription
users_collection = mongo_db.users
notification_collection = mongo_db.notifications
audit_log_collection = mongo_db.audit_log
aggregates_minutes = mongo_db.aggregates_minutes
aggregates_hourly = mongo_db.aggregates_hourly
aggregates_daily = mongo_db.aggregates_daily
user_orders_collection = mongo_db.user_orders
alerts_collection = mongo_db.alerts

portfolio_limit = 100 # Max number of portfolios a user can have
portfolio_component_limit = 25 # number of components per portfolio
profile_free_limit = 5
profile_premium_limit = 15
supported_regions = [10000002, 10000043, 10000032, 10000042, 10000030]

port = int(os.environ.get('ETF_API_PORT', 5000))
env = os.environ.get('ETF_API_ENV', 'development')

debug = False if env == 'production' else True

auth_jwt_secret = 'development' if debug else os.environ.get('ETF_API_JWT_SECRET', 'production')

# Use to connect to private backend publishing API
admin_secret = os.environ.get('ETF_API_ADMIN_SECRET', 'admin_secret')

# Application
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app)
app.secret_key = os.environ.get('ETF_API_JWT_SECRET', 'production')
CORS(app)

# Single sign on service
oauth = OAuth(app)
evesso = oauth.remote_app('evesso',
    consumer_key=os.environ.get('ETF_API_OAUTH_KEY', 'example'),
    consumer_secret=os.environ.get('ETF_API_OAUTH_SECRET', 'example'),
    request_token_params={
        'scope': '',
        'state': lambda: session['evesso_state']
    },
    base_url='https://login.eveonline.com/',
    request_token_url=None,
    access_token_method='POST',
    access_token_url='https://login.eveonline.com/oauth/token',
    authorize_url='https://login.eveonline.com/oauth/authorize'
)

# Sentry exception tracking
app.config['SENTRY_CONFIG'] = {
    'ignore_exceptions': ['KeyboardInterrupt'],
}
sentry = Sentry(app, dsn='dsn' if env == 'production' else None)

# SDE
market_ids = []
system_name_to_id = {}

with open('sde/blueprints.js', 'r', encoding='utf-8') as f:
    read_data = f.read()
    blueprints_json = read_data
    blueprints = json.loads(read_data)

with open('sde/market_groups.js', 'r', encoding='utf-8') as f:
    read_data = f.read()
    market_groups_json = read_data
    market_groups = json.loads(read_data)

    _getItems = lambda items: [x['id'] for x in items]

    def _getGroups(group, ids):
        if 'items' in group:
            ids.extend(_getItems(group['items']))

        for _group in group['childGroups']:
            _getGroups(_group, ids)

    for group in market_groups:
        _getGroups(group, market_ids)

with open('sde/market_id_to_volume.json', 'r', encoding='utf-8') as f:
    read_data = f.read()
    market_id_to_volume_json = read_data
    market_id_to_volume = dict({int(k):v for k,v in json.loads(market_id_to_volume_json).items()})

try:
    res = requests.get('https://crest-tq.eveonline.com/industry/systems/', timeout=10, headers=standard_headers)

    doc = json.loads(res.text)

    for item in doc['items']:
        system_name_to_id[item['solarSystem']['name']] = item['solarSystem']['id']

except:
    traceback.print_exc()
    exit()

re = None

try:
    re = redis.StrictRedis(host=redis_host, port=6379, db=0)
except:
    print("Redis server is unavailable")
    sentry.captureException()

# Decorator to validate a JWT and retrieve the users info from rethinkDB
# Authorization types:
#   Token <jwt>
#   Key <api_key>
def verify_jwt(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):

        user_settings = None

        try:
            auth_header = request.headers.get('Authorization')

            if auth_header == None:
                auth_header = request.headers.get('authorization')

            if auth_header == None:
                return jsonify({ 'error': "Authorization header is missing", 'code': 400 })

            split = auth_header.split(" ")

            if len(split) != 2:
                return jsonify({ 'error': "Invalid authorization header format", 'code': 400 })

            if split[0] != "Token" and split[0] != "Key":
                return jsonify({ 'error': "Authorization header must include 'Token' or 'Key'", 'code': 400 })

            if split[0] == "Token":
                try:
                    user_data = jwt.decode(split[1], auth_jwt_secret)

                    user_settings = mongo_db.settings.find_one({'user_id': user_data['user_id']})
                except jwt.exceptions.ExpiredSignatureError:
                    return jsonify({'error': "Authorization token is expired", 'code': 400})
                except jwt.exceptions.InvalidTokenError:
                    return jsonify({'error': "Authorization token is invalid", 'code': 400})
                except:
                    return jsonify({'error': "Failed to parse authorization token", 'code': 400})

            elif split[0] == "Key":

                try:
                    if split[1] is None or len(split[1]) == 0:
                        return jsonify({'error': "Unable to verify your API key. Please check that it is valid and typed correctly", 'code': 400})

                    user_settings = mongo_db.settings.find_one({'api_key': split[1]})

                    if user_settings is None:
                        return jsonify({'error': "Unable to verify your API key. Please check that it is valid and typed correctly", 'code': 400})
                except:
                    return jsonify({'error': "Failed to correctly parse api key. Please check that it is valid and typed correctly", 'code': 400})
        except:
            traceback.print_exc()
            return jsonify({ 'error': "Failed to parse authorization header", 'code': 400 })

        if user_settings is None:
            return jsonify({'error': "Failed to look up user information", 'code': 400})

        user_id = user_settings['user_id']

        return fn(user_id=user_id, settings=user_settings, *args, **kwargs)

    return wrapper

# Routes
@app.route('/', methods=['GET'])
def index():
    return current_app.send_static_file('api.html')

@app.route('/market/forecast/', methods=['GET'])
@verify_jwt
def forecast(user_id, settings):

    if settings.get('premium', False) == False:
        return jsonify({'error': "A Premium subscription is required to access this endpoint", 'code': 405})

    # Validation
    try:
        minspread = request.args.get('minspread')
        maxspread = request.args.get('maxspread')
        minvolume = request.args.get('minvolume')
        maxvolume = request.args.get('maxvolume')
        minprice = request.args.get('minprice')
        maxprice = request.args.get('maxprice')
    except:
        return jsonify({ 'error': "Invalid type used in query parameters", 'code': 400 })

    if minspread == None and maxspread == None:
        return jsonify({ 'error': "At least one of minspread and maxspread must be provided", 'code': 400 })

    if minvolume == None and maxvolume == None:
        return jsonify({ 'error': "At least one of minvolume and maxvolume must be provided", 'code': 400 })

    if minprice == None and maxprice == None:
        return jsonify({ 'error': "At least one of minprice and maxprice must be provided", 'code': 400 })

    # Further validation and default values
    try:
        if minspread:
            minspread = float(minspread)
        else:
            minspread = 0
        if maxspread:
            maxspread = float(maxspread)
        else:
            maxspread = 100
        if maxvolume:
            maxvolume = float(maxvolume)
        else:
            maxvolume = 1000000000000
        if minvolume:
            minvolume = float(minvolume)
        else:
            minvolume = 0
        if maxprice:
            maxprice = float(maxprice)
        else:
            maxprice = 1000000000000
        if minprice:
            minprice = float(minprice)
        else:
            minprice = 0
    except:
        return jsonify({ 'error': "One of the provided parameters are not a floating point or integer type.", 'code': 400 })

    # Normalize the values
    '''
    if minspread > maxspread:
        minspread = maxspread - 1
    if maxspread > minspread:
        maxspread = minspread + 1
    '''

    region = 10000002 # jita by default

    if 'region' in settings:
        region = settings['region']

    # Load data from redis cache by accessing all item id's for the given region
    pip = re.pipeline()

    for k in market_ids:
        pip.hmget('dly:%s-%s' % (k, region), ['type', 'spread_sma', 'volume_sma', 'buyPercentile', 'velocity'])

    docs = pip.execute()

    # Find ideal matches to query params
    ideal = [doc[0] for doc in docs if doc[0] is not None and doc[1] is not None and doc[2] is not None and doc[3] is not None and doc[4] is not None and float(doc[1]) >= minspread and float(doc[1]) <= maxspread and float(doc[2]) >= minvolume and float(doc[2]) <= maxvolume and float(doc[3]) >= minprice and float(doc[3]) <= maxprice ]

    # Pull out complete documents for all ideal matches
    pip = re.pipeline()

    for k in ideal:
        pip.hgetall('dly:%s-%s' % (k.decode('ascii'), region))

    # Execute and grab only wanted attributes
    docs = [{key.decode('ascii'):float(row[key]) for key in (b'type', b'spread', b'tradeVolume', b'buyPercentile', b'spread_sma', b'volume_sma', b'sellPercentile', b'velocity')} for row in pip.execute()]

    return jsonify(docs)

def regionToStationHub(region):
    return {
        10000002: 60003760,
        10000043: 60008494,
        10000032: 60011866,
        10000042: 60005686,
        10000030: 60004588
    }.get(region, 0)

@app.route('/market/forecast/regional', methods=['GET'])
@verify_jwt
def forecast_region(user_id, settings):

    min_profit = 100000

    # Validation
    try:
        start_region = int(request.args.get('start', 0))
        end_region = int(request.args.get('end', 0))
        max_volume = int(request.args.get('maxvolume', 100000))
        max_price = int(request.args.get('maxprice', 100000000))
    except:
        return jsonify({ 'error': "Invalid query parameters or missing parameter", 'code': 400 })

    if start_region == end_region:
        return jsonify({'error': "The start and end regions can't be the same", 'code': 400})

    if start_region not in supported_regions:
        return jsonify({'error': "The start region given is not supported or is invalid", 'code': 400})

    if end_region not in supported_regions:
        return jsonify({'error': "The end region given is not supported or is invalid", 'code': 400})

    if max_volume < 100:
        return jsonify({'error': "The maximum volume should be at least 100 in order to find reasonable opportunities", 'code': 400})

    if max_price < 100000:
        return jsonify({'error': "The maximum price should be at least 100000 in order to find reasonable opportunities", 'code': 400})

    start_order_map = {} # buy orders
    end_order_map = {} # sell orders

    start_hub = regionToStationHub(start_region)
    end_hub = regionToStationHub(end_region)

    for order in mongo_db.orders.find({'region': start_region, 'buy': False}, projection={'_id': False, 'time': False, 'id': False, 'region': False}):

        if order['stationID'] != start_hub and order['stationID'] < 1000000000000:
            continue

        if order['type'] in start_order_map:
            start_order_map[order['type']].append(order)
        else:
            start_order_map[order['type']] = [order]

    for order in mongo_db.orders.find({'region': end_region, 'buy': True}, projection={'_id': False, 'time': False, 'id': False, 'region': False}):

        if order['stationID'] != end_hub and order['stationID'] < 1000000000000:
            continue

        if order['type'] in end_order_map:
            end_order_map[order['type']].append(order)
        else:
            end_order_map[order['type']] = [order]

    trades = []

    # Find common type id's in the buy & sell orders for matching
    for _type in set(start_order_map.keys()).intersection(end_order_map.keys()):

        # Find the lowest sell price
        min_price = min(map(lambda doc: doc['price'],  start_order_map[_type]))

        # Filter out type's outside the given budget
        if min_price > max_price:
            continue

        #  Find the highest buy price
        max_buy = max(map(lambda doc: doc['price'],  end_order_map[_type]))

        # If profit is negligible, ignore this type
        if max_buy - min_price < min_profit:
            continue

        # Sort the list of orders by price for easy iteration
        start = sorted(start_order_map[_type], key=lambda doc: doc['price'])
        end = list(reversed(sorted(end_order_map[_type], key=lambda doc: doc['price'])))

        start_index = 0
        end_index = 0

        start_volume = start[start_index]['volume']
        end_volume = end[end_index]['volume']

        while end_index < len(end) and start_index < len(start):

            # Missing item id
            if _type not in market_id_to_volume:
                break

            if end[end_index]['price'] - start[start_index]['price'] < min_profit:
                break

            needed_volume = market_id_to_volume[_type]
            max_per_trade = math.floor(max_volume / needed_volume)

            # If a single of this item exceeds the max weight given, skip it
            if needed_volume > max_volume:
                break

            if start_volume >= end_volume:

                count = end_volume

                if count > max_per_trade:
                    count = max_per_trade

                if count * start[start_index]['price'] > max_price:
                    count = math.floor(max_price / start[start_index]['price'])

                end_volume = 0
                start_volume -= count

            elif start_volume < end_volume:

                count = start_volume

                if count > max_per_trade:
                    count = max_per_trade

                if count * start[start_index]['price'] > max_price:
                    count = math.floor(max_price / start[start_index]['price'])

                end_volume -= count
                start_volume = 0

            if count <= 0:
                break

            trades.append({
                'totalProfit': (end[end_index]['price'] - start[start_index]['price']) * count,
                'perProfit': end[end_index]['price'] - start[start_index]['price'],
                'perVolumeProfit': (end[end_index]['price'] - start[start_index]['price']) / needed_volume,
                'quantity': count,
                'volume': count * needed_volume,
                'type': _type,
                'buyPrice': start[start_index]['price'],
                'sellPrice': end[end_index]['price'],
            })

            if end_volume <= 0:

                end_index += 1

                if end_index < len(end):
                    end_volume = end[end_index]['volume']

            if start_volume <= 0:
                start_index += 1

                if start_index < len(start):
                    start_volume = start[start_index]['volume']

    return jsonify(trades)

@app.route('/market/current/<int:region>/<int:typeid>', methods=['GET'])
@verify_jwt
def market_current(region, typeid, user_id, settings):

    if settings.get('api_access', False) == False:
        return jsonify({'error': "Active API access subscription is required to access this endpoint", 'code': 405})

    if isinstance(typeid, int) == False:
        return jsonify({ 'error': "Required parameter 'typeID' is not a valid integer", 'code': 400 })

    if isinstance(region, int) == False:
        return jsonify({ 'error': "Required parameter 'region' is not a valid integer", 'code': 400 })

    if region not in supported_regions:
        return jsonify({ 'error': "The provided region %s is not supported" % region, 'code': 400 })

    if re.exists('cur:'+str(typeid)+'-'+str(region)) == False:
        return jsonify({ 'error': "Failed to find current market data for the given typeID and region", 'code': 400 })

    reDoc = re.hgetall('cur:'+str(typeid)+'-'+str(region))

    return jsonify({key.decode('ascii'):float(reDoc[key]) for key in (b'type', b'spread', b'tradeVolume', b'buyPercentile', b'sellPercentile')})

@app.route('/market/history/minutes/<int:typeid>', methods=['GET'])
@verify_jwt
def market_history_minutes(typeid, user_id, settings):

    if settings.get('api_access', False) == False:
        return jsonify({'error': "Active API access subscription is required to access this endpoint", 'code': 405})

    if isinstance(typeid, int) == False:
        return jsonify({ 'error': "Required parameter 'typeID' is not a valid integer", 'code': 400 })

    data = list(aggregates_minutes.find({'type': typeid}, fields={'_id': False}))

    for d in data:
        d['time'] = d['time'].isoformat()

    return jsonify(data)

@app.route('/market/history/hourly/<int:typeid>', methods=['GET'])
@verify_jwt
def market_history_hourly(typeid, user_id, settings):

    if settings.get('api_access', False) == False:
        return jsonify({'error': "Active API access subscription is required to access this endpoint", 'code': 405})

    if isinstance(typeid, int) == False:
        return jsonify({ 'error': "Required parameter 'typeID' is not a valid integer", 'code': 400 })

    data = list(aggregates_hourly.find({'type': typeid}, fields={'_id': False}))

    for d in data:
        d['time'] = d['time'].isoformat()

    return jsonify(data)

@app.route('/market/history/daily/<int:typeid>', methods=['GET'])
@verify_jwt
def market_history_daily(typeid, user_id, settings):

    if settings.get('api_access', False) == False:
        return jsonify({'error': "Active API access subscription is required to access this endpoint", 'code': 405})

    if isinstance(typeid, int) == False:
        return jsonify({ 'error': "Required parameter 'typeID' is not a valid integer", 'code': 400 })

    data = list(aggregates_daily.find({'type': typeid}, fields={'_id': False}))

    for d in data:
        d['time'] = d['time'].isoformat()

    return jsonify(data)

@app.route('/market/orders/self', methods=['GET'])
@verify_jwt
def market_self_orders(user_id, settings):

    if settings.get('api_access', False) == False:
        return jsonify({'error': "Active API access subscription is required to access this endpoint", 'code': 405})

    try:
        orders = list(user_orders_collection.find({'user_id': user_id}, fields={'_id': False}))

        if orders is None or len(orders) == 0:
            raise Exception()

    except Exception:
        traceback.print_exc()
        return jsonify([])

    return jsonify(orders)

@app.route('/portfolio/create', methods=['POST'])
@verify_jwt
def create_portfolio(user_id, settings):

    try:
        if request.is_json == False:
            return jsonify({'error': "Request Content-Type header must be set to 'application/json'", 'code': 400})

    except:
        return jsonify({ 'error': "There was a problem parsing your json request", 'code': 400 })

    if settings.get('premium', False) == False:
        return jsonify({'error': "A Premium subscription is required to access this endpoint", 'code': 405})

    if 'name' not in request.json:
        return jsonify({ 'error': "Required parameter 'name' is missing", 'code': 400 })
    if 'description' not in request.json:
        return jsonify({ 'error': "Required parameter 'description' is missing", 'code': 400 })
    if 'type' not in request.json:
        return jsonify({ 'error': "Required parameter 'type' is missing", 'code': 400 })
    if 'components' not in request.json:
        return jsonify({ 'error': "Required parameter 'components' is missing", 'code': 400 })

    name = request.json['name']
    description = request.json['description']
    _type = request.json['type']
    components = request.json['components']
    efficiency = 0

    if 'efficiency' in request.json:
        efficiency = request.json['efficiency']

    build_system = request.json.get('buildSystem', None)
    sell_price = request.json.get('sellPrice', None)

    if isinstance(name, str) == False:
        return jsonify({ 'error': "Required parameter 'name' is not a valid string", 'code': 400 })
    if isinstance(description, str) == False:
        return jsonify({ 'error': "Required parameter 'description' is not a valid string", 'code': 400 })
    if isinstance(_type, int) == False:
        return jsonify({ 'error': "Required parameter 'type' is not a valid integer", 'code': 400 })
    if isinstance(components, list) == False:
        return jsonify({ 'error': "Required parameter 'components' is not a valid array", 'code': 400 })
    if isinstance(efficiency, int) == False:
        return jsonify({ 'error': "Optional parameter 'efficiency' is not a valid integer", 'code': 400 })

    if _type is not 0 and _type is not 1:
        return jsonify({ 'error': "Portfolio type must be 0 for Trading Portfolio or 1 for Industry Portfolio", 'code': 400 })

    if efficiency < 0 or efficiency > 100:
        return jsonify({ 'error': "Optional parameter 'efficiency' must be between 0 and 100", 'code': 400 })

    if _type == 1:
        if len(components) > 1:
            return jsonify({ 'error': "Industry portfolios must have a single manufacturable component", 'code': 400 })
    else:
        if len(components) > portfolio_component_limit:
            return jsonify({ 'error': "The limit for the number of components in a portfolio is %s" % portfolio_component_limit, 'code': 400 })

    if build_system is not None:
        if build_system not in system_name_to_id:
            return jsonify({'error': "Selected build system '%s' is invalid; check for correct spelling & case-sensitiveness" % build_system, 'code': 400})

    if sell_price is not None:
        if (isinstance(sell_price, int) == False or isinstance(sell_price, float) == False) and sell_price <= 0:
            return jsonify({'error': "Override sell price should not be negative or zero", 'code': 400})

    used_ids = []
    _components = []
    industryQuantity = 0
    industryTypeID = 0
    manufacturedQuantity = 0

    try:
        if len(components) == 0:
            return jsonify({ 'error': "There are no components in your request", 'code': 400 })

        for component in components:
            if isinstance(component, dict) == False:
                return jsonify({ 'error': "Components must be vaild objects", 'code': 400 })

            if 'typeID' not in component:
                return jsonify({ 'error': "Component is missing required 'typeID' parameter", 'code': 400 })
            if 'quantity' not in component:
                return jsonify({ 'error': "Component is missing required 'quantity' parameter", 'code': 400 })

            if len(component.keys()) > 2:
                return jsonify({ 'error': "Component has invalid parameters", 'code': 400 })

            typeID = component['typeID']
            quantity = component['quantity']

            if isinstance(typeID, int) == False:
                return jsonify({ 'error': "Component 'typeID' is not a valid integer", 'code': 400 })

            if isinstance(quantity, int) == False:
                return jsonify({ 'error': "Component 'quantity' is not a valid integer", 'code': 400 })

            if typeID in used_ids:
                return jsonify({ 'error': "Component 'typeID' is duplicated. Each component must be unique", 'code': 400 })

            if typeID < 0 or typeID > 100000:
                return jsonify({ 'error': "Component 'typeID' is outside a reasonable range", 'code': 400 })

            if quantity < 0 or quantity > 1000000000:
                return jsonify({ 'error': "Component 'quantity' is outside a reasonable range", 'code': 400 })

            # Trading components will use the user supplied components and not duplicates
            if _type == 0:
                if str(typeID) not in market_ids:
                    return jsonify({ 'error': "Component 'typeID' is not a valid market item", 'code': 400 })

                used_ids.append(typeID)
                _components.append({'typeID': typeID, 'quantity': quantity})

            # Industry components are auto-selected based on the manufactured component the user requested
            else:
                if str(typeID) not in blueprints:
                    return jsonify({ 'error': "Component 'typeID' is not a valid manufacturable item", 'code': 400 })

                _blueprint = blueprints[str(typeID)]

                # Multiply the component requirements by the number of runs
                # Also consider the material efficiency
                for comp in _blueprint['materials']:
                    _components.append({'typeID': comp['typeID'], 'quantity':    math.ceil(comp['quantity'] * quantity * ((100.0 - efficiency) / 100.0))})

                industryQuantity = quantity
                industryTypeID = typeID

                # Multiply the manufactured quantity by the quantiy of the component the user is tracking
                # So if its 5 missile blueprints that each manufacture 100, the total quantiy is 500
                manufacturedQuantity = _blueprint['quantity'] * quantity

    except:
        traceback.print_exc()
        return jsonify({ 'error': "There is an error in the components array or the component is invalid", 'code': 400 })

    userPortfolioCount = portfolio_collection.find({'user_id': user_id}).count()

    if userPortfolioCount >= portfolio_limit:
        return jsonify({ 'error': "There is a limit of %s portfolios that a user can create. If you need this limit raised, contact an EVE Exchange admin." % portfolio_limit, 'code': 400 })

    try:
        portfolioCount = portfolio_collection.find().count()
        if portfolioCount > 0:
            portfolioMax = list(portfolio_collection.find().sort('portfolioID', DESCENDING))[0]
            portfolioID = 1 if portfolioMax is None else portfolioMax['portfolioID'] + 1
        else:
            portfolioID = 1

        portfolio_doc = {
            'name': name,
            'description': description,
            'type': _type,
            'efficiency': efficiency,
            'components': _components,
            'user_id': user_id,
            'portfolioID': portfolioID,
            'time': datetime.utcnow(),
            'hourlyChart': [],
            'dailyChart': [],
            'currentValue': 0,
            'averageSpread': 0,
            'industryQuantity': industryQuantity,
            'industryTypeID': industryTypeID,
            'industryValue': 0,
            'startingValue': 0,
            'manufacturedQuantity': manufacturedQuantity,
            'overrideSellPrice': 0 if sell_price is None else sell_price,
            'buildSystem': 0 if build_system is None else system_name_to_id[build_system]
        }

        portfolio_collection.insert(portfolio_doc)

        requests.post('http://localhost:4501/publish/portfolios/%s' % user_id, timeout=1)

        audit_log_collection.insert({
            'user_id': user_id,
            'target': portfolioID,
            'balance': 0,
            'action': 5,
            'time': datetime.now()
        })

        requests.post('http://localhost:4501/publish/audit', timeout=1)

    except:
        traceback.print_exc()
        return jsonify({ 'error': "There was an error with creating your portfolio", 'code': 400 })

    return jsonify({ 'message': 'Your new portfolio has been created with an id of %s' % portfolioID })

@app.route('/portfolio/delete/<int:id>', methods=['POST'])
@verify_jwt
def portfolio_delete(id, user_id, settings):

    if settings.get('premium', False) == False:
        return jsonify({'error': "A Premium subscription is required to access this endpoint", 'code': 405})

    try:
        portfolio = portfolio_collection.find_one({'user_id': user_id, 'portfolioID': id})

        if portfolio is None:
            raise Exception()

    except:
        return jsonify({ 'error': "Failed to look up your portfolio. Double check that you have the correct portfolio ID", 'code': 400 })

    try:
        portfolio_collection.remove({'user_id': user_id, 'portfolioID': id}, multi=False)

        requests.post('http://localhost:4501/publish/portfolios/%s' % user_id, timeout=1)

        audit_log_collection.insert({
            'user_id': user_id,
            'target': id,
            'balance': 0,
            'action': 6,
            'time': datetime.now()
        })

        requests.post('http://localhost:4501/publish/audit', timeout=1)

    except Exception:
        traceback.print_exc()
        return jsonify({ 'error': "There was a database error while processing your deletion request", 'code': 400 })

    return jsonify({ 'message': 'Your portfolio has been deleted' })

@app.route('/portfolio/get/<int:id>', methods=['GET'])
@verify_jwt
def portfolio_get_single(id, user_id, settings):

    if settings.get('api_access', False) == False:
        return jsonify({'error': "Active API access subscription is required to access this endpoint", 'code': 405})

    try:
        portfolio = portfolio_collection.find_one({'user_id': user_id, 'portfolioID': id}, fields={'_id': False, 'hourlyChart': False, 'dailyChart': False})

        if portfolio is None:
            raise Exception()

    except:
        return jsonify({ 'error': "Failed to look up your portfolio. Double check that you have the correct portfolio ID and that you have permissions for it", 'code': 400 })

    portfolio['time'] = portfolio.get('time', datetime.utcnow()).isoformat()

    return jsonify(portfolio)

@app.route('/portfolio/get/all', methods=['GET'])
@verify_jwt
def portfolio_get_all(user_id, settings):

    if settings.get('api_access', False) == False:
        return jsonify({'error': "Active API access subscription is required to access this endpoint", 'code': 405})

    try:
        portfolios = list(portfolio_collection.find({'user_id': user_id}, fields={'_id': False, 'hourlyChart': False, 'dailyChart': False}))

        if portfolios is None or len(portfolios) == 0:
            raise Exception()

    except:
        return jsonify({ 'error': "Failed to look up your portfolios. Double check that you've created at least one portfolio", 'code': 400 })

    for p in portfolios:
        p['time'] = p.get('time', datetime.utcnow()).isoformat()

    return jsonify(portfolios)

@app.route('/portfolio/get/<int:id>/multibuy', methods=['GET'])
@verify_jwt
def portfolio_get_multibuy(id, user_id, settings):

    try:
        portfolio = portfolio_collection.find_one({'user_id': user_id, 'portfolioID': id}, projection={'_id': False, 'hourlyChart': False, 'dailyChart': False})

        if portfolio is None:
            raise Exception()

    except:
        return jsonify({ 'error': "Failed to look up your portfolio. Double check that you have the correct portfolio ID and that you have permissions for it", 'code': 400 })

    # Validation
    try:
        start_region = int(request.args.get('region', 10000002))
        quantity = int(request.args.get('quantity', 1500))
    except:
        return jsonify({ 'error': "Invalid query parameters or missing parameter", 'code': 400 })

    total_cost = 0
    start_hub = regionToStationHub(start_region)
    type_to_order_map = {}
    type_to_component = {}
    type_to_result = {}
    components = portfolio.get('components', [])

    pip = re.pipeline()

    for component in components:
        type_to_component[component['typeID']] = component

        len = re.llen('ord_cnt:%s-%s' % (component['typeID'], start_region))

        for k in re.lrange('ord_cnt:%s-%s' % (component['typeID'], start_region), 0, len):

            pip.hmget('ord:%s' % k.decode('ascii'), ['volume', 'buy', 'price', 'stationID', 'type'])

    rows = pip.execute()

    for row in rows:

        if row[0] == None or row[1] == None or row[2] == None or row[3] == None or row[4] == None:
            continue

        if row[1] == b'True':
            continue

        # Ignore orders outside the hub or in citadels
        if int(row[3]) != start_hub and int(row[3]) < 1000000000000:
            continue

        if int(row[4]) not in type_to_order_map:
            type_to_order_map[int(row[4])] = []

        type_to_order_map[int(row[4])].append({
            'volume': float(row[0]),
            'price': float(row[2])
        })

    for _type in type_to_component:

        cost = 0
        orders = sorted(type_to_order_map[_type], key=lambda doc: doc['price']) if _type in type_to_order_map else []
        volume_required = type_to_component[_type]['quantity'] * quantity
        available = 0
        type_to_result[_type] = {
            'price': 0,
            'defecit': 0,
            'wanted': volume_required,
            'available': 0
        }

        for order in orders:

            available += order['volume']

            if volume_required == 0:
                continue

            if order['volume'] > volume_required:
                cost += order['price'] * volume_required
                volume_required = 0
            else:
                volume_required -= order['volume']
                cost += order['price'] * order['volume']

        if volume_required > 0:
            type_to_result[_type]['defecit'] = volume_required

        type_to_result[_type]['price'] = cost
        type_to_result[_type]['available'] = available
        total_cost += cost

    return jsonify({
        'components': type_to_result,
        'totalCost': total_cost
    })

@app.route('/subscription/subscribe', methods=['POST'])
@verify_jwt
def subscription_subscribe(user_id, settings):

    subscription = None
    cost = 150000000

    try:
        subscription = subscription_collection.find_one({'user_id': user_id})

        if subscription is None:
            raise Exception()
    except:
        traceback.print_exc()
        return jsonify({ 'error': "Failed to look up your subscription status", 'code': 400 })

    is_premium = subscription['premium']

    if is_premium is not None:
        if is_premium == True:
            return jsonify({ 'error': "Your current subscription status is already premium", 'code': 400 })

    balance = subscription['balance']

    if cost > balance:
     return jsonify({ 'error': "Insufficient balance", 'code': 400 })

    try:
        subscription_collection.find_and_modify({'user_id': user_id}, {
            '$set': {
                'premium': True,
                'subscription_date': datetime.utcnow()
            },
            '$inc': {
                'balance': -cost
            },
            '$push': {
                'history': {
                    'time': datetime.utcnow(),
                    'type': 1,
                    'amount': cost,
                    'description': 'Subscription fee',
                    'processed': True
                }
            }
        })

        settings_collection.find_and_modify({'user_id': user_id}, {
            '$set': {
                'premium': True,
            },
        })

        requests.post('http://localhost:4501/publish/subscription/%s' % user_id, timeout=1)

        audit_log_collection.insert({
            'user_id': user_id,
            'target': 0,
            'balance': 0,
            'action': 2,
            'time': datetime.now()
        })

        requests.post('http://localhost:4501/publish/audit', timeout=1)

    except Exception:
        traceback.print_exc()
        return jsonify({ 'error': "There was a database error while processing your subscription request. This should be reported", 'code': 400 })

    return jsonify({ 'message': 'Your subscription status has been updated' })

@app.route('/subscription/unsubscribe', methods=['POST'])
@verify_jwt
def subscription_unsubscribe(user_id, settings):

    if settings.get('premium', False) == False:
        return jsonify({'error': "A Premium subscription is required to access this endpoint", 'code': 405})

    subscription = None

    try:
        subscription = subscription_collection.find_one({'user_id': user_id})

        if subscription is None:
            raise Exception()
    except:
        return jsonify({ 'error': "Failed to look up your subscription status", 'code': 400 })

    try:
        subscription_collection.find_and_modify({'user_id': user_id}, {
            '$set': {
                'premium': False,
                'subscription_date': None,
                'api_access': False
            }
        })

        settings_collection.find_and_modify({'user_id': user_id}, {
            '$set': {
                'premium': False,
                'api_access': False
            },
        })

        requests.post('http://localhost:4501/publish/subscription/%s' % user_id, timeout=1)

        audit_log_collection.insert({
            'user_id': user_id,
            'target': 0,
            'balance': 0,
            'action': 3,
            'time': datetime.now()
        })

        requests.post('http://localhost:4501/publish/audit', timeout=1)

    except Exception:
        traceback.print_exc()
        return jsonify({ 'error': "There was a database error while processing your subscription request. This should be reported", 'code': 400 })

    return jsonify({ 'message': 'Your subscription status has been updated' })

@app.route('/subscription/withdraw/<int:amount>', methods=['POST'])
@verify_jwt
def subscription_withdraw_amount(amount, user_id, settings):

    subscription = None

    try:
        subscription = subscription_collection.find_one({'user_id': user_id})

        if subscription is None:
            raise Exception()
    except:
        return jsonify({ 'error': "Failed to look up your subscription status", 'code': 400 })

    balance = subscription['balance']

    if amount < 0 or amount > balance:
     return jsonify({ 'error': "Insufficient balance", 'code': 400 })

    try:
        subscription_collection.find_and_modify({'user_id': user_id}, {
            '$inc': {
                'balance': -amount
            },
            '$push': {
                'history': {
                    'time': datetime.utcnow(),
                    'type': 1,
                    'amount': amount,
                    'description': 'Manual withdrawal request',
                    'processed': False
                }
            }
        })

        requests.post('http://localhost:4501/publish/subscription/%s' % user_id, timeout=1)

        audit_log_collection.insert({
            'user_id': user_id,
            'target': 0,
            'balance': amount,
            'action': 10,
            'time': datetime.now()
        })

        requests.post('http://localhost:4501/publish/audit', timeout=1)

    except Exception:
        traceback.print_exc()
        return jsonify({ 'error': "There was a database error while processing your withdrawal request. This should be reported", 'code': 400 })

    return jsonify({ 'message': 'Your withdrawal request has been submitted' })

@app.route('/subscription/api/enable', methods=['POST'])
@verify_jwt
def api_access_enable(user_id, settings):

    if settings.get('premium', False) == False:
        return jsonify({'error': "A Premium subscription is required to access this endpoint", 'code': 400})

    cost = 150000000
    pro_rate = 5000000

    try:
        subscription = subscription_collection.find_one({'user_id': user_id})

        if subscription is None:
            raise Exception()
    except:
        traceback.print_exc()
        return jsonify({ 'error': "Failed to look up your subscription status", 'code': 400 })

    if (subscription.get('api_access', False)) == True:
        return jsonify({'error': "API access is already enabled on your account", 'code': 400})

    balance = subscription['balance']

    subscription_expires = subscription['subscription_date'] + timedelta(days=30)

    days_pro_rate = subscription_expires - datetime.utcnow()

    cost = cost - max(0, min(cost, int(pro_rate * (29 - days_pro_rate.days))))

    if cost > balance:
     return jsonify({ 'error': "Insufficient balance", 'code': 400 })

    try:
        subscription_collection.find_and_modify({'user_id': user_id}, {
            '$set': {
                'api_access': True
            },
            '$inc': {
                'balance': -cost
            },
            '$push': {
                'history': {
                    'time': datetime.utcnow(),
                    'type': 1,
                    'amount': cost,
                    'description': 'API access enabling fee',
                    'processed': True
                }
            }
        })

        settings_collection.find_and_modify({'user_id': user_id}, {
            '$set': {
                'api_access': True,
            },
        })

        requests.post('http://localhost:4501/publish/subscription/%s' % user_id, timeout=1)

        audit_log_collection.insert({
            'user_id': user_id,
            'target': 0,
            'balance': cost,
            'action': 12,
            'time': datetime.now()
        })

        requests.post('http://localhost:4501/publish/audit', timeout=1)

    except Exception:
        traceback.print_exc()
        return jsonify({ 'error': "There was a database error while processing your subscription request. This should be reported", 'code': 400 })

    return jsonify({ 'message': 'API access has been enabled on your account' })

@app.route('/subscription/api/disable', methods=['POST'])
@verify_jwt
def api_access_disable(user_id, settings):

    if settings.get('premium', False) == False:
        return jsonify({'error': "A Premium subscription is required to access this endpoint", 'code': 405})

    subscription = None

    try:
        subscription = subscription_collection.find_one({'user_id': user_id})

        if subscription is None:
            raise Exception()
    except:
        return jsonify({ 'error': "Failed to look up your subscription status", 'code': 400 })

    if subscription.get('api_access', False) == False:
        return jsonify({'error': "API access is currently not enabled on your account", 'code': 400})

    try:
        subscription_collection.find_and_modify({'user_id': user_id}, {
            '$set': {
                'api_access': False
            }
        })

        settings_collection.find_and_modify({'user_id': user_id}, {
            '$set': {
                'api_access': False
            },
        })

        requests.post('http://localhost:4501/publish/subscription/%s' % user_id, timeout=1)

        audit_log_collection.insert({
            'user_id': user_id,
            'target': 0,
            'balance': 0,
            'action': 13,
            'time': datetime.now()
        })

        requests.post('http://localhost:4501/publish/audit', timeout=1)

    except Exception:
        traceback.print_exc()
        return jsonify({ 'error': "There was a database error while processing your subscription request. This should be reported", 'code': 400 })

    return jsonify({ 'message': 'API access has been disabled for your account' })

@app.route('/notification/<string:not_id>/read', methods=['POST'])
@verify_jwt
def notification_set_read(not_id, user_id, settings):

    notification = None

    try:
        notification = notification_collection.find_one({'_id': ObjectId(oid=not_id), 'user_id': user_id})

        if notification is None:
            raise Exception()
    except:
        return jsonify({ 'error': "Failed to look up the notification %s" % not_id, 'code': 400 })

    try:
        notification_collection.find_and_modify({'_id': ObjectId(oid=not_id), 'user_id': user_id}, {
            '$set': {
                'read': True
            }
        })

        requests.post('http://localhost:4501/publish/notifications/%s' % user_id, timeout=1)

    except Exception:
        traceback.print_exc()
        return jsonify({ 'error': "There was a database error while updating your notification. This should be reported", 'code': 400 })

    return jsonify({ 'message': 'Notification status is updated' })

@app.route('/notification/all/read', methods=['POST'])
@verify_jwt
def notification_all_read(user_id, settings):

    try:
        notification_collection.update({'user_id': user_id}, {
            '$set': {
                'read': True
            }
        }, multi=True)

        requests.post('http://localhost:4501/publish/notifications/%s' % user_id, timeout=1)

    except Exception:
        traceback.print_exc()
        return jsonify({ 'error': "There was a database error while updating your notification. This should be reported", 'code': 400 })

    return jsonify({ 'message': 'Notification statuses are updated' })

@app.route('/notification/<string:not_id>/unread', methods=['POST'])
@verify_jwt
def notification_set_unread(not_id, user_id, settings):

    notification = None

    try:
        notification = notification_collection.find_one({'_id': ObjectId(oid=not_id), 'user_id': user_id})

        if notification is None:
            raise Exception()
    except:
        return jsonify({ 'error': "Failed to look up the notification %s" % not_id, 'code': 400 })

    try:
        notification_collection.find_and_modify({'_id': ObjectId(oid=not_id), 'user_id': user_id}, {
            '$set': {
                'read': False
            }
        })

        requests.post('http://localhost:4501/publish/notifications/%s' % user_id, timeout=1)

    except Exception:
        traceback.print_exc()
        return jsonify({ 'error': "There was a database error while updating your notification. This should be reported", 'code': 400 })

    return jsonify({ 'message': 'Notification status is updated' })


@app.route('/notification/get/all', methods=['GET'])
@verify_jwt
def notification_get_all(user_id, settings):

    try:
        notifications = list(notification_collection.find({'user_id': user_id}))

        if notifications == None:
            return jsonify([])

    except Exception:
        traceback.print_exc()
        return jsonify([])

    for n in notifications:
        n['id'] = str(n['_id'])
        del n['_id']
        n['time'] = n['time'].isoformat()

    return jsonify(notifications)

# API Keys

@app.route('/apikey/add', methods=['POST'])
@verify_jwt
def apikey_add(user_id, settings):

    try:
        if request.is_json == False:
            return jsonify({'error': "Request Content-Type header must be set to 'application/json'", 'code': 400})

    except:
        return jsonify({ 'error': "There was a problem parsing your json request", 'code': 400 })

    if settings.get('premium', False) == False and len(settings['profiles']) >= profile_free_limit:
        return jsonify({'error': "You've reached the profile limit of %s for free users. Upgrade to premium for an additional %s profiles" % (profile_free_limit, profile_premium_limit-profile_free_limit), 'code': 400})
    elif len(settings['profiles']) >= profile_premium_limit:
        return jsonify({'error': "You've reached the profile limit of %s" % profile_premium_limit, 'code': 400})

    if 'type' not in request.json:
        return jsonify({ 'error': "Required parameter 'type' is missing", 'code': 400 })
    if 'keyID' not in request.json:
        return jsonify({ 'error': "Required parameter 'keyID' is missing", 'code': 400 })
    if 'vCode' not in request.json:
        return jsonify({ 'error': "Required parameter 'vCode' is missing", 'code': 400 })

    keyID = request.json['keyID']
    vCode = request.json['vCode']
    type = request.json['type']

    if isinstance(keyID, str) == False:
        return jsonify({ 'error': "Required parameter 'keyID' is not a valid string", 'code': 400 })
    if isinstance(vCode, str) == False:
        return jsonify({ 'error': "Required parameter 'vCode' is not a valid string", 'code': 400 })
    if isinstance(type, int) == False:
        return jsonify({ 'error': "Required parameter 'type' is not a valid integer", 'code': 400 })

    if type is not 0 and type is not 1:
        return jsonify({ 'error': "type must be 0 for character key or 1 for corporation key", 'code': 400 })

    if type == 0 and 'characterID' not in request.json:
        return jsonify({ 'error': "Required parameter 'characterID' is missing", 'code': 400 })
    elif type == 0:
        characterID = request.json['characterID']

        if isinstance(characterID, int) == False:
            return jsonify({'error': "Required parameter 'characterID' is not a valid integer", 'code': 400})
    else:
        characterID = 0

    if type == 1 and settings.get('premium', False) == False:
        return jsonify({'error': "A premium subscription is required to add a corporation API key", 'code': 400})

    if type == 1 and 'walletKey' not in request.json:
        return jsonify({'error': "Required parameter 'walletKey' is missing", 'code': 400})
    elif type == 1:
        walletKey = request.json['walletKey']

        if isinstance(walletKey, int) == False:
            return jsonify({'error': "Required parameter 'walletKey' is not a valid integer", 'code': 400})

        if walletKey < 1000 or walletKey > 1006:
            return jsonify({'error': "Corporation wallet division key must be 1000 - 1006", 'code': 400})
    else:
        walletKey = 0

    characterName = ""
    corporationID = 0
    corporationName = ""

    # Grab key info from EVE and verify it matches
    try:
        res = requests.get('https://api.eveonline.com/account/APIKeyInfo.xml.aspx?keyID=%s&vCode=%s' % (keyID, vCode), timeout=10, headers=standard_headers)

        tree = ET.fromstring(res.text)

        if tree.find('error') is not None:
            raise Exception()

        keyResult = tree.find('result').find('key')
        rows = list(keyResult.find('rowset'))

        # TODO: Check expiry & correct corp mask
        if type == 0:
            if keyResult.attrib['type'] != "Character" and keyResult.attrib['type'] != "Account":
                return jsonify({'error': "Failed to verify that this key is the correct type of 'Character' or 'Account'", 'code': 400})
            #if keyResult.attrib['accessMask'] != "23072779":
            #    return jsonify({'error': "Failed to verify that the access mask is 23072779", 'code': 400})
            found = False
            for row in rows:
                if row.attrib['characterID'] == str(characterID):
                    found = True
                    characterName = row.attrib['characterName']
                    corporationName = row.attrib['corporationName']
                    corporationID = 0 if row.attrib['corporationID'] == "0" else int(row.attrib['corporationID'])
                    break
            if found is False:
                return jsonify({'error': "The requested characterID is not associated with this api key", 'code': 400})
        else:
            if keyResult.attrib['type'] != "Corporation":
                return jsonify({'error': "Failed to verify that this key is the correct type of 'Corporation'", 'code': 400})
            if keyResult.attrib['accessMask'] != "3149835":
                return jsonify({'error': "Failed to verify that the access mask is 3149835", 'code': 400})
            row = rows[0] # there is only 1 entry in this case
            characterID = int(row.attrib['characterID'])
            characterName = row.attrib['characterName']
            corporationName = row.attrib['corporationName']
            corporationID = int(row.attrib['corporationID'])

    except:
        traceback.print_exc()
        return jsonify({'error': "Failed to verify the given keyID and vCode", 'code': 400})

    # Verify that the api key the user is adding does not already exist
    if 'profiles' not in settings:
        return jsonify({'error': "Your account is not set up yet to accept API keys. Try re-logging into the website or report this to Maxim Stride", 'code': 400})

    for key in settings['profiles']:

        if keyID == key['key_id'] and vCode == key['vcode']:
            return jsonify({'error': "The given api key is already attached to your account", 'code': 400})

        if type == 0:
            if characterID == key['character_id'] and key['type'] == 0:
                return jsonify({'error': "The given character already has an API key on your account", 'code': 400})
        else:
            if corporationID == key['corporation_id'] and key['type'] == 1:
                return jsonify({'error': "The given corporation already has an API key on your account", 'code': 400})

    # Sanity checks
    if characterID is 0:
        return jsonify({'error': "There was an unknown problem grabbing the correct information for this api key", 'code': 400})

    # Add api key to account now
    settings_collection.find_and_modify({'user_id': user_id}, {
            '$push': {
                'profiles': {
                    'type': type,
                    'key_id': keyID,
                    'vcode': vCode,
                    'character_id': characterID,
                    'character_name': characterName,
                    'corporation_id': corporationID,
                    'corporation_name': corporationName,
                    'wallet_balance': 0,
                    'wallet_key': walletKey,
                    'id': str(ObjectId()) # unique ID to be used by api's
                }
            }
    })

    audit_log_collection.insert({
        'user_id': user_id,
        'target': keyID,
        'balance': 0,
        'action': 7,
        'time': datetime.now()
    })

    try:
        requests.post('http://localhost:4501/publish/settings/%s' % user_id, timeout=1)
        requests.post('http://localhost:4501/publish/audit', timeout=1)
    except:
        traceback.print_exc()
        sentry.captureException()

    return jsonify({'message': 'API key has been added to your account'})

@app.route('/apikey/remove/<string:key_id>', methods=['POST'])
@verify_jwt
def apikey_remove(key_id, user_id, settings):

    if isinstance(key_id, str) == False:
        return jsonify({ 'error': "Required parameter 'id' is not a valid string", 'code': 400 })

    # Verify that the api key the user is adding does not already exist
    if 'profiles' not in settings:
        return jsonify({'error': "Your account is not set up yet to accept API keys. Try re-logging into the website or report this to Maxim Stride", 'code': 400})

    found = False

    # Verify the requested key exists
    try:
        for key in settings['profiles']:
            if 'id' in key and key_id == key['id']:
                found = True

    except:
        return jsonify({'message': 'There was a problem removing the API key. Please report this to Maxim Stride'})

    if not found:
        return jsonify({'message': 'Failed to find the requested API key. Make sure to use its unique ID in the request'})

    # Query to remove the exact array element
    settings_collection.find_and_modify({'user_id': user_id}, {
            '$pull': {
                'profiles': {
                    'id': key_id
                }
            }
    })

    audit_log_collection.insert({
        'user_id': user_id,
        'target': key_id,
        'balance': 0,
        'action': 8,
        'time': datetime.now()
    })

    try:
        requests.post('http://localhost:4501/publish/settings/%s' % user_id, timeout=1)
        requests.post('http://localhost:4501/publish/audit', timeout=1)
    except:
        traceback.print_exc()
        sentry.captureException()

    return jsonify({'message': 'API key has been removed from your account'})

@app.route('/apikey/get', methods=['GET'])
@verify_jwt
def apikey_get_all(user_id, settings):

    # Verify that the api key the user is adding does not already exist
    if 'profiles' not in settings:
        return jsonify({'error': "Your account is not set up yet to accept API keys. Try re-logging into the website or report this to Maxim Stride", 'code': 400})

    return jsonify(settings['profiles'])

@app.route('/apikey/get/<string:key_id>', methods=['GET'])
@verify_jwt
def apikey_get_one(key_id, user_id, settings):

    if isinstance(key_id, str) == False:
        return jsonify({ 'error': "Required parameter 'id' is not a valid string", 'code': 400 })

    # Verify that the api key the user is adding does not already exist
    if 'profiles' not in settings:
        return jsonify({'error': "Your account is not set up yet to accept API keys. Try re-logging into the website or report this to Maxim Stride", 'code': 400})

    try:
        for key in settings['profiles']:
            if 'id' in key and key_id == key['id']:
                return jsonify(key)

    except:
        sentry.captureException()
        return jsonify({'message': 'There was a problem retrieving the API key. Please report this to Maxim Stride'})

    return jsonify({'message': 'Failed to find the requested API key. Make sure to use its unique ID in the request'})

@app.route('/settings/save', methods=['POST'])
@verify_jwt
def settings_savee(user_id, settings):

    try:
        if request.is_json == False:
            return jsonify({'error': "Request Content-Type header must be set to 'application/json'", 'code': 400})

    except:
        return jsonify({ 'error': "There was a problem parsing your json request", 'code': 400 })

    try:
        pinned_charts = request.json.get('pinned_charts', [])

        # All market settings
        market = request.json.get('market', None)

        if market is None:
            return jsonify({'error': "There are important settings missing from your request", 'code': 400})

        region = market.get('region', 10000002)
        default_tab = market.get('default_tab', 0)
        default_timespan = market.get('default_timespan', 0)
        simulation_broker_fee = market.get('simulation_broker_fee', 0)
        simulation_sales_tax = market.get('simulation_sales_tax', 0)
        simulation_margin = market.get('simulation_margin', 0)
        simulation_strategy = market.get('simulation_strategy', 0)
        simulation_margin_type = market.get('simulation_margin_type', 0)
        simulation_overhead = market.get('simulation_overhead', 0)
        simulation_wanted_profit = market.get('simulation_wanted_profit', 0)
        ticker_watchlist = market.get('ticker_watchlist', [])

        if region not in supported_regions:
            return jsonify({'error': "Region is not a valid value", 'code': 400})

        if (isinstance(simulation_broker_fee, int) == False or isinstance(simulation_broker_fee, float) == False) and 0 > simulation_broker_fee > 100:
            return jsonify({'error': "Simulation Broker Fee is not a valid value", 'code': 400})

        if (isinstance(simulation_sales_tax, int) == False or isinstance(simulation_sales_tax, float) == False) and 0 > simulation_sales_tax > 100:
            return jsonify({'error': "Simulation Sales Tax is not a valid value", 'code': 400})

        if isinstance(simulation_margin_type, int) == False and 0 > simulation_margin_type > 1:
            return jsonify({'error': "Simulation Margin Type is not a valid value", 'code': 400})

        if simulation_margin_type == 0:
            if (isinstance(simulation_margin, int) == False or isinstance(simulation_margin, float) == False) and 0 > simulation_margin > 100000000000:
                return jsonify({'error': "Simulation Margin is not a valid value", 'code': 400})
        else:
            if (isinstance(simulation_margin, int) == False or isinstance(simulation_margin, float) == False) and 0 > simulation_margin > 100:
                return jsonify({'error': "Simulation Margin is not a valid value", 'code': 400})

        if isinstance(simulation_strategy, int) == False and 0 > simulation_strategy > 1:
            return jsonify({'error': "Simulation Strategy is not a valid value", 'code': 400})

        if (isinstance(simulation_overhead, int) == False or isinstance(simulation_overhead, float) == False) and 0 > simulation_overhead > 100000000000:
            return jsonify({'error': "Simulation Overhead is not a valid value", 'code': 400})

        if (isinstance(simulation_wanted_profit, int) == False or isinstance(simulation_wanted_profit, float) == False) and 0 > simulation_wanted_profit > 100:
            return jsonify({'error': "Simulation Wanted Profit is not a valid value", 'code': 400})

        if isinstance(ticker_watchlist, list) == False:
            return jsonify({'error': "Ticker watchlist is not a valid list", 'code': 400})

        # General settings
        general = request.json.get('general', None)

        if general is None:
            return jsonify({'error': "There are important settings missing from your request", 'code': 400})

        auto_renew = general.get('auto_renew', True)

        if isinstance(auto_renew, bool) == False:
            return jsonify({'error': "Auto Renew is not a valid value", 'code': 400})

        # Chart visuals
        chart_visuals = request.json.get('chart_visuals', None)

        if chart_visuals is None:
            return jsonify({'error': "There are important settings missing from your request", 'code': 400})

        price = chart_visuals.get('price', True)
        spread = chart_visuals.get('spread', True)
        spread_sma = chart_visuals.get('spread_sma', True)
        volume = chart_visuals.get('volume', True)
        volume_sma = chart_visuals.get('volume_sma', True)

        if isinstance(price, bool) == False:
            return jsonify({'error': "Price is not a valid value", 'code': 400})

        if isinstance(spread, bool) == False:
            return jsonify({'error': "Spread is not a valid value", 'code': 400})

        if isinstance(spread_sma, bool) == False:
            return jsonify({'error': "Spread SMA is not a valid value", 'code': 400})

        if isinstance(volume, bool) == False:
            return jsonify({'error': "Volume is not a valid value", 'code': 400})

        if isinstance(volume, bool) == False:
            return jsonify({'error': "Volume is not a valid value", 'code': 400})

        if isinstance(volume_sma, bool) == False:
            return jsonify({'error': "Volume SMA is not a valid value", 'code': 400})

        # Guidebook
        guidebook = request.json.get('guidebook', None)

        if guidebook is None:
            return jsonify({'error': "There are important settings missing from your request", 'code': 400})

        disable = guidebook.get('disable', False)
        profiles = guidebook.get('profiles', True)
        market_browser = guidebook.get('market_browser', True)
        forecast = guidebook.get('forecast', True)
        portfolios = guidebook.get('portfolios', True)
        tickers = guidebook.get('tickers', True)
        subscription = guidebook.get('subscription', True)

        if isinstance(disable, bool) == False:
            disable = False

        if isinstance(profiles, bool) == False:
            profiles = False

        if isinstance(market_browser, bool) == False:
            market_browser = False

        if isinstance(forecast, bool) == False:
            forecast = False

        if isinstance(portfolios, bool) == False:
            portfolios = False

        if isinstance(subscription, bool) == False:
           subscription = False

        if isinstance(tickers, bool) == False:
           tickers = False

        # Forecast
        forecast_saved = request.json.get('forecast', None)

        if forecast_saved is None:
            return jsonify({'error': "There are important settings missing from your request", 'code': 400})

        min_volume = forecast_saved.get('min_volume', 50)
        max_volume = forecast_saved.get('max_volume', 200)
        min_spread = forecast_saved.get('min_spread', 10)
        max_spread = forecast_saved.get('max_spread', 20)
        min_buy = forecast_saved.get('min_buy', 5000000)
        max_buy = forecast_saved.get('max_buy', 75000000)

        if (isinstance(min_volume, int) == False or isinstance(min_volume, float) == False) and 0 > min_volume > 100000000:
            min_volume = None

        if (isinstance(max_volume, int) == False or isinstance(max_volume, float) == False) and 0 > min_volume > 100000000:
            max_volume = None

        if (isinstance(min_spread, int) == False or isinstance(min_spread, float) == False) and 0 > min_spread > 100:
            min_spread = None

        if (isinstance(max_spread, int) == False or isinstance(max_spread, float) == False) and 0 > min_buy > 100:
            max_spread = None

        if (isinstance(min_buy, int) == False or isinstance(min_buy, float) == False) and 0 > min_buy > 100000000000:
            min_buy = None

        if (isinstance(max_buy, int) == False or isinstance(max_buy, float) == False) and 0 > min_buy > 100000000000:
            max_buy = None

        forecast_regional = request.json.get('forecast_regional', None)

        if forecast_regional is None:
            return jsonify({'error': "There are important settings missing from your request", 'code': 400})

        max_regional_volume = forecast_regional.get('max_volume', 100000)
        max_regional_price = forecast_saved.get('max_price', 1000000000)
        start_region = forecast_saved.get('start_region', 10000043)
        end_region = forecast_saved.get('end_region', 10000002)

        if (isinstance(max_regional_volume, int) == False or isinstance(max_regional_volume, float) == False) and 100 > min_volume > 10000000:
            max_regional_volume = None

        if (isinstance(max_regional_price, int) == False or isinstance(max_regional_price, float) == False) and 100000 > max_regional_price > 1000000000000:
            max_regional_price = None

        if start_region not in supported_regions:
            start_region = None

        if end_region not in supported_regions:
            end_region = None

        # Alerts
        alerts_saved = request.json.get('alerts', None)

        if alerts_saved is None:
            return jsonify({'error': "There are important settings missing from your request", 'code': 400})

        show_browser = alerts_saved.get('canShowBrowserNotification', True)
        send_evemail = alerts_saved.get('canSendMailNotification', True)

        if isinstance(show_browser, bool) == False:
            show_browser = True

        if isinstance(send_evemail, bool) == False:
            send_evemail = True

    except:
        traceback.print_exc()
        sentry.captureException()
        return jsonify({'error': "There was a problem with parsing your settings", 'code': 400})

    try:
        mongo_db.settings.find_and_modify({'user_id': user_id}, {
            '$set': {
                'pinned_charts': pinned_charts,
                'market': {
                    'region': region,
                    'default_tab': default_tab,
                    'default_timespan': default_timespan,
                    'simulation_broker_fee': simulation_broker_fee,
                    'simulation_sales_tax': simulation_sales_tax,
                    'simulation_margin': simulation_margin,
                    'simulation_strategy': simulation_strategy,
                    'simulation_margin_type': simulation_margin_type,
                    'simulation_overhead': simulation_overhead,
                    'simulation_wanted_profit': simulation_wanted_profit,
                    'ticker_watchlist': ticker_watchlist
                },
                'general': {
                    'auto_renew': auto_renew
                },
                'chart_visuals': {
                    'price': price,
                    'spread': spread,
                    'spread_sma': spread_sma,
                    'volume': volume,
                    'volume_sma': volume_sma
                },
                'guidebook': {
                    'disable': disable,
                    'profiles': profiles,
                    'market_browser': market_browser,
                    'forecast': forecast,
                    'portfolios': portfolios,
                    'subscription': subscription,
                    'tickers': tickers
                },
                'forecast': {
                    'min_volume': min_volume,
                    'max_volume': max_volume,
                    'min_spread': min_spread,
                    'max_spread': max_spread,
                    'min_buy': min_buy,
                    'max_buy': max_buy
                },
                'forecast_regional': {
                    'max_volume': max_regional_volume,
                    'max_price': max_regional_price,
                    'start_region': start_region,
                    'end_region': end_region
                },
                'alerts': {
                    'canShowBrowserNotification': show_browser,
                    'canSendMailNotification': send_evemail
                }
            }
        })
    except:
        return jsonify({'error': "There was a problem with saving your settings", 'code': 400})

    return jsonify({'message': 'New settings have been applied'})

@app.route('/alerts/create', methods=['POST'])
@verify_jwt
def create_alert(user_id, settings):

    try:
        if request.is_json == False:
            return jsonify({'error': "Request Content-Type header must be set to 'application/json'", 'code': 400})

    except:
        return jsonify({ 'error': "There was a problem parsing your json request", 'code': 400 })

    try:
        req_options = {
            'alertType': int,
            'frequency': int
        }

        price_alert_options = {
            'priceAlertPriceType': int,
            'priceAlertComparator': int,
            'priceAlertAmount': [int, float],
            'priceAlertItemID': int,
        }

        sales_alert_options = {
            'salesAlertType': int,
            'salesAlertProfile': int
        }

        if 'alertType' not in request.json:
            return jsonify({ 'error': "Alert type is missing from your request", 'code': 400 })

        alert_type = request.json['alertType']

        if not isinstance(alert_type, int):
            return jsonify({ 'error': "The following option is not the correct data type: alertType", 'code': 400 })
        if alert_type < 0 or alert_type > 1:
            return jsonify({ 'error': "The following option is missing from your request or is incorrect: alertType", 'code': 400 })

        for k in req_options:
            if k not in request.json:
                return jsonify({ 'error': "The following option is missing from your request: %s" % k, 'code': 400 })
            if not isinstance(request.json[k], req_options[k]):
                return jsonify({ 'error': "The following option is not the correct data type: %s" % k, 'code': 400 })

        if request.json['frequency'] > 720:
            return jsonify({ 'error': "Alert frequency should be a lower number (in hours)", 'code': 400 })
        if request.json['frequency'] < 0:
            return jsonify({ 'error': "Alert frequency should not be a negative number", 'code': 400 })

        if alert_type == 0:

            for k in price_alert_options:
                if k not in request.json:
                    return jsonify({ 'error': "The following option is missing from your request: %s" % k, 'code': 400 })
                if isinstance(price_alert_options[k], list):
                    valid = next((x for x in price_alert_options[k] if isinstance(request.json[k], x)), False)
                    if valid == False:
                        return jsonify({ 'error': "The following option is not the correct data type: %s" % k, 'code': 400 })
                else:
                    if not isinstance(request.json[k], price_alert_options[k]):
                        return jsonify({ 'error': "The following option is not the correct data type: %s" % k, 'code': 400 })

            new_alert = {
                **{k:request.json[k] for k in req_options.keys()},
                **{k:request.json[k] for k in price_alert_options.keys()}
            }

            if new_alert['priceAlertPriceType'] < 0 or new_alert['priceAlertPriceType'] > 3:
                return jsonify({ 'error': "Price alert type is invalid", 'code': 400 })
            if new_alert['priceAlertComparator'] < 0 or new_alert['priceAlertComparator'] > 2:
                return jsonify({ 'error': "Price comparator type is invalid", 'code': 400 })
            if str(new_alert['priceAlertItemID']) not in market_ids:
                return jsonify({ 'error': "Price alert item id is invalid", 'code': 400 })
            if new_alert['priceAlertAmount'] == 0:
                return jsonify({ 'error': "priceAlertAmount should not be 0", 'code': 400 })

        elif alert_type == 1:

            for k in sales_alert_options:
                if k not in request.json:
                    return jsonify({ 'error': "The following option is missing from your request: %s" % k, 'code': 400 })
                if not isinstance(request.json[k], sales_alert_options[k]):
                    return jsonify({ 'error': "The following option is not the correct data type: %s" % k, 'code': 400 })

            new_alert = {
                **{k:request.json[k] for k in req_options.keys()},
                **{k:request.json[k] for k in sales_alert_options.keys()}
            }

        new_alert['user_id'] = user_id
        new_alert['nextTrigger'] = datetime.utcnow()
        new_alert['paused'] = False
        new_alert['createdAt'] = datetime.utcnow()
        new_alert['lastTrigger'] = None

        alerts_collection.insert(new_alert)

        audit = {
            'user_id': user_id,
            'target': alert_type,
            'balance': new_alert['priceAlertItemID'] if alert_type == 0 else 0,
            'action': 16,
            'time': datetime.utcnow()
        }

        audit_log_collection.insert(audit)

        requests.post('http://localhost:4501/publish/alerts/%s' % user_id, timeout=1)
        # Audit log
        requests.post('http://localhost:4501/publish/audit', timeout=1)

    except:
        traceback.print_exc()
        return jsonify({'error': "There was a problem with saving your alert", 'code': 400})

    if new_alert:
        new_alert['_id'] = str(new_alert['_id'])
        return jsonify(new_alert)
    
    return jsonify({'error': "There was a problem with saving your alert", 'code': 400})

@app.route('/alerts/toggle/<string:id>', methods=['GET'])
@verify_jwt
def alert_toggle(id, user_id, settings):

    try:
        alert = alerts_collection.find_one({'user_id': user_id, '_id': ObjectId(oid=id)})

        if alert is None:
            raise Exception()

    except:
        return jsonify({ 'error': "Failed to look up the requested alert", 'code': 400 })
    
    new_state = False if alert['paused'] == True else True

    alerts_collection.find_and_modify({'user_id': user_id, '_id': ObjectId(oid=id)}, {
        '$set': {
            'paused': new_state
        }
    })

    requests.post('http://localhost:4501/publish/alerts/%s' % user_id, timeout=1)

    return jsonify({'message': "Alert %s is %s" % (id, 'now paused' if new_state == True else 'no longer paused')})

@app.route('/alerts/reset/<string:id>', methods=['GET'])
@verify_jwt
def alert_reset(id, user_id, settings):

    try:
        alert = alerts_collection.find_one({'user_id': user_id, '_id': ObjectId(oid=id)})

        if alert is None:
            raise Exception()

    except:
        return jsonify({ 'error': "Failed to look up the requested alert", 'code': 400 })
    
    new_state = False if alert['paused'] == True else True

    alerts_collection.find_and_modify({'user_id': user_id, '_id': ObjectId(oid=id)}, {
        '$set': {
            'nextTrigger': datetime.utcnow()
        }
    })

    requests.post('http://localhost:4501/publish/alerts/%s' % user_id, timeout=1)

    return jsonify({'message': "Alert delay has been reset"})

@app.route('/alerts/remove/<string:id>', methods=['GET'])
@verify_jwt
def alert_remove(id, user_id, settings):

    try:
        alert = alerts_collection.find_one({'user_id': user_id, '_id': ObjectId(oid=id)})

        if alert is None:
            raise Exception()

    except:
        return jsonify({ 'error': "Failed to look up the requested alert", 'code': 400 })

    try:
        alerts_collection.remove({'user_id': user_id, '_id': ObjectId(oid=id)}, multi=False)

        audit = {
            'user_id': user_id,
            'target': id,
            'balance': 0,
            'action': 17,
            'time': datetime.utcnow()
        }

        audit_log_collection.insert(audit)

        requests.post('http://localhost:4501/publish/audit', timeout=1)

    except:
        return jsonify({ 'error': "There was a problem removing the given alert", 'code': 400 })

    requests.post('http://localhost:4501/publish/alerts/%s' % user_id, timeout=1)

    return jsonify({'message': "Alert %s has been removed" % id})

# SDE - deprecated

@app.route('/sde/blueprints', methods=['GET'])
def sde_blueprints():

    return Response(blueprints_json)

@app.route('/sde/marketgroups', methods=['GET'])
def sde_marketgroups():

    return Response(market_groups_json)

# OAuth

@app.route('/oauth')
def do_oauth():
    session['evesso_state'] = gen_salt(10)
    return evesso.authorize(callback=url_for('do_oauth_authorized', _external=True))

@app.route('/oauth/verify')
def do_oauth_authorized():
    state = request.args.get('state')

    if not state or session.get('evesso_state') != state:
        return jsonify({'error': "State was not preserved during SSO requests", 'code': 400})

    del session['evesso_state']

    resp = evesso.authorized_response()
    if resp is None or resp.get('access_token') is None:
        return 'Access denied: reason=%s error=%s resp=%s' % (
            request.args['error'],
            request.args['error_description'],
            resp
        )

    session['evesso_token'] = (resp['access_token'], '')

    me = evesso.get('oauth/verify')

    token = jwt.encode({'user_id': me.data['CharacterID'],
                        'user_name': me.data['CharacterName'],
                        'exp': datetime.utcnow() + timedelta(days=7)
    }, auth_jwt_secret, algorithm='HS256')

    return redirect('%s/?token=%s' % (redirect_host, token.decode('ascii')), code=302)

@app.route('/search/systems', methods=['GET'])
def search_systems():

    # Validation
    try:
        name = request.args.get('name', None)
    except:
        return jsonify({ 'error': "Invalid type used in query parameters", 'code': 400 })

    if name is None:
        return jsonify({'error': "No search string was provided", 'code': 400})

    tokens = process.extract(name, system_name_to_id.keys(), limit=6, scorer=fuzz.ratio)

    results = [{'name': token[0], 'id': system_name_to_id[token[0]]} for token in tokens]

    return jsonify(results)

@evesso.tokengetter
def get_evesso_oauth_token():
    return session.get('evesso_token')

# Deepstream

def insert_defaults(user_id, user_name):
    user_doc = {
        'user_id': user_id,
        'user_name': user_name,
        'admin': False,
        'last_online': datetime.now(),
        'join_date': datetime.now()
    }

    settings_doc = {
        'user_id': user_id,
        'premium': True,
        'api_key': str(ObjectId()),
        'profiles': [],
        'market': {
            'region': 10000002
        },
        'general': {
            'auto_renew': True
        },
        'chart_visuals': {
            'price': True,
            'spread': True,
            'spread_sma': True,
            'volume': True,
            'volume_sma': True
        },
        'guidebook': {
            'disable': False,
            'subscription': True,
            'portfolios': True,
            'forecast': True,
            'profiles': True,
            'market_browser': True
        },
        'forecast': {
            'min_buy': 5000000,
            'max_buy': 75000000,
            'min_spread': 10,
            'max_spread': 20,
            'min_volume': 50,
            'max_volume': 200
        },
        'forecast_regional': {
            'max_volume': 100000,
            'max_price': 1000000000,
            'start_region': 10000043,
            'end_region': 10000002
        },
        'alerts': {
            'canShowBrowserNotification': True,
            'canSendMailNotification': True
        }
    }

    profit_alltime = {
        "alltime": {
            "broker": 0,
            "profit": 0,
            "taxes": 0
        },
        "biannual": {
            "broker": 0,
            "profit": 0,
            "taxes": 0
        },
        "day": {
            "broker": 0,
            "profit": 0,
            "taxes": 0
        },
        "month": {
            "broker": 0,
            "profit": 0,
            "taxes": 0
        },
        "user_id": user_id,
        "week": {
            "broker": 0,
            "profit": 0,
            "taxes": 0
        }
    }

    profit_items = {
        "user_id": user_id,
        "items": [],
        'profiles': []
    }

    profit_chart = {
        'frequency': 'hourly',
        'broker': 0,
        'time': datetime.utcnow(),
        'taxes': 0,
        'profit': 0,
        'user_id': user_id
    }

    subscription_doc = {
        "user_id": user_id,
        "premium": True,
        "balance": 0,
        "history": [],
        "subscription_date": datetime.utcnow() - timedelta(days=23),
        "user_name": user_name
    }

    beta_notification = {
        "user_id": user_id,
        "time": datetime.utcnow(),
        "read": False,
        "message": "Welcome to EVE Exchange! A 7 day premium trial has been automatically activated. Any questions can be forwarded to Maxim Stride or @maxim on Tweetfleet. Happy trading."
    }

    audit = {
        'user_id': user_id,
        'target': 0,
        'balance': 0,
        'action': 11,
        'time': datetime.now()
    }

    mongo_db.users.insert(user_doc)
    mongo_db.settings.insert(settings_doc)
    mongo_db.profit_alltime.insert(profit_alltime)
    mongo_db.profit_top_items.insert(profit_items)
    mongo_db.subscription.insert(subscription_doc)
    mongo_db.notifications.insert(beta_notification)
    mongo_db.audit_log.insert(audit)
    mongo_db.profit_chart.insert(profit_chart)

    # Publish the new account creation
    requests.post('http://localhost:4501/publish/subscription/%s' % user_id, timeout=1)

    # Audit log
    requests.post('http://localhost:4501/publish/audit', timeout=1)

    return user_doc, settings_doc

@app.route('/deepstream/authorize', methods=['POST'])
def do_deepstream_authorize():

    try:
        if request.is_json == False:
            return jsonify({'error': "Request Content-Type header must be set to 'application/json'", 'code': 400})
    except:
        return 'Invalid request format', 403

    _data = None
    user_doc = None
    settings_doc = None

    try:
        if 'authData' not in request.json:
            return 'Invalid credentials', 403

        if 'admin' in request.json['authData']:
            if request.json['authData']['admin'] == admin_secret:
                return jsonify({'username': 'admin', 'clientData': {'user_name':'admin'}, 'serverData': {'admin':True}})

        if 'token' not in request.json['authData']:
            return 'Invalid credentials', 403

        if isinstance(request.json['authData']['token'], str) == False:
            return 'Invalid credentials', 403

        _data = jwt.decode(request.json['authData']['token'], auth_jwt_secret)

        user_doc = mongo_db.users.find_one({'user_id': _data['user_id']})

        if user_doc is None:

            # Create new user data
            try:
                user_doc, settings_doc = insert_defaults(_data['user_id'], _data['user_name'])

                if '_id' in settings_doc:
                    del settings_doc['_id']
            except:
                sentry.captureException()
                traceback.print_exc()
                return 'Invalid credentials', 403

            # Publish new user
            requests.post('http://localhost:4501/user/create', json=settings_doc, timeout=1)
        else:
            settings_doc = mongo_db.settings.find_one({'user_id': _data['user_id']})
            mongo_db.users.update({'_id': user_doc['_id']}, { '$set': { 'last_online': datetime.now()}})

        try:
            requests.post('http://localhost:4501/user/login', json={'user_id': _data['user_id']}, timeout=1)
        except:
            sentry.captureException()
            traceback.print_exc()
            return 'There was a problem contacting the server', 400

        # ID object can't be serialized to json
        if '_id' in user_doc:
            del user_doc['_id']
        if '_id' in settings_doc:
            del settings_doc['_id']
    except jwt.exceptions.ExpiredSignatureError:
        return 'Session has expired', 403
    except:
        sentry.captureException()
        traceback.print_exc()
        return 'Invalid credentials', 403

    if _data is None or user_doc is None or settings_doc is None:
        return 'Invalid credentials', 403

    client_data = {
        **{k:user_doc[k] for k in ['user_name', 'user_id', 'admin']},
        **{k:settings_doc[k] for k in ['premium']},
    }

    return jsonify({ 'username': _data['user_name'], 'clientData': client_data, 'serverData': {**user_doc, **settings_doc}})

# Error handlers
@app.errorhandler(404)
def not_found(error):
    return jsonify({ 'error': "Route not found", 'code': 404 })

@app.errorhandler(403)
def validation_error(error):
    return jsonify({ 'error': "Failed to validate your authentication token or api key", 'code': 403 })

@app.errorhandler(401)
def auth_error(error):
    return jsonify({ 'error': "Failed to validate your authentication token or api key", 'code': 401 })

@app.errorhandler(405)
def not_allowed(error):
    return jsonify({ 'error': "Method or endpoint is not allowed", 'code': 405 })

# Start server
if __name__ == '__main__':
    if debug:
        app.run(debug=debug, port=port, host='0.0.0.0', threaded=False)
    else:
        print("Running in production WSGI mode on port %s" % port)
        http_server = WSGIServer(('', port), app)
        http_server.serve_forever()
