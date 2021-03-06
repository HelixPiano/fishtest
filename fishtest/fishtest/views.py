import copy
import datetime
import os
import smtplib
import time
import threading
import re
import html

from email.mime.text import MIMEText
from collections import defaultdict

import requests

import scipy
import scipy.stats
import numpy

from pyramid.security import remember, forget, authenticated_userid, has_permission
from pyramid.view import view_config, forbidden_view_config
from pyramid.httpexceptions import HTTPFound, exception_response
from pyramid.response import Response

import fishtest.stats.stat_util


FISH_URL = 'https://tests.stockfishchess.org/tests/view/'


def clear_cache():
  global last_time, last_tests
  building.acquire()
  last_time = 0
  last_tests = None
  building.release()


def cached_flash(request, requestString):
  clear_cache()
  request.session.flash(requestString)
  return


@view_config(route_name='home', renderer='mainpage.mak')
def mainpage(request):
  return HTTPFound(location=request.route_url('tests'))


@view_config(route_name='login', renderer='mainpage.mak',
             require_csrf=True, request_method=('GET', 'POST'))
@forbidden_view_config(renderer='mainpage.mak')
def login(request):
  login_url = request.route_url('login')
  referrer = request.url
  if referrer == login_url:
    referrer = '/'  # never use the login form itself as came_from
  came_from = request.params.get('came_from', referrer)

  if request.method == 'POST':
    username = request.POST.get('username')
    password = request.POST.get('password')
    token = request.userdb.authenticate(username, password)
    if 'error' not in token:
      headers = remember(request, username)
      return HTTPFound(location=came_from, headers=headers)

    request.session.flash(token['error'])  # 'Incorrect password'
  return {}


@view_config(route_name='logout', require_csrf=True, request_method='POST')
def logout(request):
  session = request.session
  headers = forget(request)
  session.invalidate()
  return HTTPFound(location=request.route_url('tests'), headers=headers)


@view_config(route_name='signup', renderer='signup.mak',
             require_csrf=True, request_method=('GET', 'POST'))
def signup(request):
  if request.method != 'POST':
    return {}
  if len(request.POST.get('password', '')) == 0:
    request.session.flash('Non-empty password required')
    return {}
  if request.POST.get('password') != request.POST.get('password2', ''):
    request.session.flash('Matching verify password required')
    return {}
  if '@' not in request.POST.get('email', ''):
    request.session.flash('Email required')
    return {}
  if len(request.POST.get('username', '')) == 0:
    request.session.flash('Username required')
    return {}
  if not request.POST.get('username', '').isalnum():
    request.session.flash('Alphanumeric username required')
    return {}

  path = os.path.expanduser('~/fishtest.captcha.secret')
  if os.path.exists(path):
    with open(path, 'r') as f:
      secret = f.read()
      payload = {'secret': secret,
                 'response': request.POST.get('g-recaptcha-response', ''),
                 'remoteip': request.remote_addr}
      response = requests.post(
          'https://www.google.com/recaptcha/api/siteverify',
          data=payload).json()
      if 'success' not in response or not response['success']:
        if 'error-codes' in response:
          print(response['error-codes'])
        request.session.flash('Captcha failed')
        return {}

  result = request.userdb.create_user(
    username=request.POST.get('username', ''),
    password=request.POST.get('password', ''),
    email=request.POST.get('email', '')
  )
  if not result:
    request.session.flash('Invalid username')
  else:
    request.session.flash(
        'Your account will be activated by an administrator soon...')
    return HTTPFound(location=request.route_url('login'))
  return {}


def delta_date(date):
  if date != datetime.datetime.min:
    diff = datetime.datetime.utcnow() - date
    if diff.days != 0:
      delta = '%d days ago' % (diff.days)
    elif diff.seconds / 3600 > 1:
      delta = '%d hours ago' % (diff.seconds / 3600)
    elif diff.seconds / 60 > 1:
      delta = '%d minutes ago' % (diff.seconds / 60)
    else:
      delta = 'seconds ago'
  else:
    delta = 'Never'
  return delta


def parse_tc(tc):
  # Total time for a game is assumed to be the double of tc for each player
  # reduced for 70% becuase on average game is stopped earlier. For instance
  # in case of 60+0.05 time for each player is 62 secs, so the game duration
  # is 62*2*70%
  scale = 2 * 0.90

  # Parse the time control in cutechess format
  if tc == '15+0.05':
    return 17.0 * scale

  if tc == '60+0.05':
    return 62.0 * scale

  chunks = tc.split('+')
  increment = 0.0
  if len(chunks) == 2:
    increment = float(chunks[1])

  chunks = chunks[0].split('/')
  num_moves = 0
  if len(chunks) == 2:
    num_moves = int(chunks[0])

  time_tc = chunks[-1]
  chunks = time_tc.split(':')
  if len(chunks) == 2:
    time_tc = float(chunks[0]) * 60 + float(chunks[1])
  else:
    time_tc = float(chunks[0])

  if num_moves > 0:
    time_tc = time_tc * (40.0 / num_moves)
  return (time_tc + (increment * 40.0)) * scale


@view_config(route_name='actions', renderer='actions.mak')
def actions(request):
  search_action = request.params.get('action', '')
  search_user = request.params.get('user', '')

  actions_list = []
  for action in request.actiondb.get_actions(100, search_action, search_user):
    item = {
      'action': action['action'],
      'time': action['time'],
      'username': action['username'],
    }
    if action['action'] == 'update_stats':
      item['user'] = ''
      item['description'] = 'Update user statistics'
    elif action['action'] == 'block_user':
      item['description'] = (
          'blocked' if action['data']['blocked'] else 'unblocked')
      item['user'] = action['data']['user']
    elif action['action'] == 'modify_run':
      item['run'] = action['data']['before']['args']['new_tag']
      item['_id'] = action['data']['before']['_id']
      item['description'] = []

      before = action['data']['before']['args']['priority']
      after = action['data']['after']['args']['priority']
      if before != after:
        item['description'].append(
            'priority changed from %s to %s' % (before, after))

      before = action['data']['before']['args']['num_games']
      after = action['data']['after']['args']['num_games']
      if before != after:
        item['description'].append(
            'games changed from %s to %s' % (before, after))

      before = action['data']['before']['args']['throughput']
      after = action['data']['after']['args']['throughput']
      if before != after:
        item['description'].append(
            'throughput changed from %s to %s' % (before, after))

      item['description'] = 'modify: ' + ', '.join(item['description'])
    else:
      item['run'] = action['data']['args']['new_tag']
      item['_id'] = action['data']['_id']
      item['description'] = ' '.join(action['action'].split('_'))
      if action['action'] == 'stop_run':
        item['description'] += ': %s' % (
            action['data'].get('stop_reason', 'User stop'))

    actions_list.append(item)

  return {'actions': actions_list,
          'approver': has_permission('approve_run', request.context, request)}


def get_idle_users(request):
  idle = {}
  for u in request.userdb.get_users():
    idle[u['username']] = u
  for u in request.userdb.user_cache.find():
    del idle[u['username']]
  idle = list(idle.values())
  return idle


@view_config(route_name='pending', renderer='pending.mak')
def pending(request):
  if not has_permission('approve_run', request.context, request):
    request.session.flash('You cannot view pending users')
    return HTTPFound(location=request.route_url('tests'))

  return {'users': request.userdb.get_pending(),
          'idle': get_idle_users(request)}


@view_config(route_name='user', renderer='user.mak')
@view_config(route_name='profile', renderer='user.mak')
def user(request):
  userid = authenticated_userid(request)
  if not userid:
    request.session.flash('Please login')
    return HTTPFound(location=request.route_url('login'))
  user_name = request.matchdict.get('username', userid)
  profile = (user_name == userid)
  if not profile and not has_permission(
      'approve_run', request.context, request):
    request.session.flash('You cannot inspect users')
    return HTTPFound(location=request.route_url('tests'))
  user_data = request.userdb.get_user(user_name)
  if 'user' in request.POST:
    if profile:
      if len(request.params.get('password')) > 0:
        if (request.params.get('password')
            != request.params.get('password2', '')):
          request.session.flash('Matching verify password required')
          return {'user': user_data, 'profile': profile}
        user_data['password'] = request.params.get('password')
      if len(request.params.get('email')) > 0:
        user_data['email'] = request.params.get('email')
    else:
      user_data['blocked'] = ('blocked' in request.POST)
      request.userdb.last_pending_time = 0
      request.actiondb.block_user(authenticated_userid(request),
                              {'user': user_name, 'blocked': user_data['blocked']})
      request.session.flash(('Blocked' if user_data['blocked'] else 'Unblocked')
                            + ' user ' + user_name)
    request.userdb.save_user(user_data)
    return HTTPFound(location=request.route_url('tests'))
  userc = request.userdb.user_cache.find_one({'username': user_name})
  hours = int(userc['cpu_hours']) if userc is not None else 0
  return {'user': user_data, 'limit': request.userdb.get_machine_limit(user_name),
          'hours': hours, 'profile': profile}


@view_config(route_name='users', renderer='users.mak')
def users(request):
  users_list = list(request.userdb.user_cache.find())
  users_list.sort(key=lambda k: k['cpu_hours'], reverse=True)
  return {'users': users_list}


@view_config(route_name='users_monthly', renderer='users.mak')
def users_monthly(request):
  users_list = list(request.userdb.top_month.find())
  users_list.sort(key=lambda k: k['cpu_hours'], reverse=True)
  return {'users': users_list}


def get_master_bench():
  bs = re.compile(r'(^|\s)[Bb]ench[ :]+([0-9]{7})', re.MULTILINE)
  for c in requests.get(
      'https://api.github.com/repos/official-stockfish/Stockfish/commits').json():
    if not 'commit' in c:
      return None
    m = bs.search(c['commit']['message'])
    if m:
      return m.group(2)
  return None


def get_sha(branch, repo_url):
  """ Resolves the git branch to sha commit """
  api_url = repo_url.replace('https://github.com',
                             'https://api.github.com/repos')
  commit = requests.get(api_url + '/commits/' + branch).json()
  if 'sha' in commit:
    return commit['sha'], commit['commit']['message'].split('\n')[0]
  else:
    return '', ''


def parse_spsa_params(raw, spsa):
  params = []
  for line in raw.split('\n'):
    chunks = line.strip().split(',')
    if len(chunks) == 0:
      continue
    if len(chunks) != 6:
      raise Exception('"%s" needs 6 parameters"' % (line))
    param = {
      'name': chunks[0],
      'start': float(chunks[1]),
      'min': float(chunks[2]),
      'max': float(chunks[3]),
      'c_end': float(chunks[4]),
      'r_end': float(chunks[5]),
    }
    param['c'] = param['c_end'] * spsa['num_iter'] ** spsa['gamma']
    param['a_end'] = param['r_end'] * param['c_end'] ** 2
    param['a'] = param['a_end'] * (spsa['A'] + spsa['num_iter']) ** spsa['alpha']
    param['theta'] = param['start']
    params.append(param)
  return params


def validate_form(request):
  data = {
    'base_tag': request.POST['base-branch'],
    'new_tag': request.POST['test-branch'],
    'tc': request.POST['tc'],
    'book': request.POST['book'],
    'book_depth': request.POST['book-depth'],
    'base_signature': request.POST['base-signature'],
    'new_signature': request.POST['test-signature'],
    'base_options': request.POST['base-options'],
    'new_options': request.POST['new-options'],
    'username': authenticated_userid(request),
    'tests_repo': request.POST['tests-repo'],
    'info': request.POST['run-info'],
  }

  def strip_message(m):
    s = re.sub(r"[Bb]ench[ :]+[0-9]{7}\s*", "", m)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n+", r"\n", s)
    return s.rstrip()

  # Fill new_signature/info from commit info if left blank
  if len(data['new_signature']) == 0 or len(data['info']) == 0:
    api_url = data['tests_repo'].replace('https://github.com',
                                         'https://api.github.com/repos')
    api_url += ('/commits' + '/' + data['new_tag'])
    c = requests.get(api_url).json()
    if 'commit' not in c:
      raise Exception('Cannot find branch in developer repository')
    if len(data['new_signature']) == 0:
      bs = re.compile(r'(^|\s)[Bb]ench[ :]+([0-9]{7})', re.MULTILINE)
      m = bs.search(c['commit']['message'])
      if m:
        data['new_signature'] = m.group(2)
    if len(data['info']) == 0:
        data['info'] = ('' if re.match('^[012]?[0-9][^0-9].*', data['tc'])
                        else 'LTC: ') + strip_message(c['commit']['message'])

  # Check that the book exists in the official books repo
  if len(data['book']) > 0:
    api_url = 'https://api.github.com/repos/official-stockfish/books/contents'
    c = requests.get(api_url).json()
    matcher = re.compile(r'\.(epd|pgn)\.zip$')
    valid_book_filenames = [file['name'] for file in c if matcher.search(file['name'])]
    if data['book'] + '.zip' not in valid_book_filenames:
      raise Exception('Invalid book - ' + data['book'])

  if len([v for v in list(data.values()) if len(v) == 0]) > 0:
    raise Exception('Missing required option')

  data['auto_purge'] = request.POST.get('auto-purge') is not None

  # In case of reschedule use old data,
  # otherwise resolve sha and update user's tests_repo
  if 'resolved_base' in request.POST:
    data['resolved_base'] = request.POST['resolved_base']
    data['resolved_new'] = request.POST['resolved_new']
    data['msg_base'] = request.POST['msg_base']
    data['msg_new'] = request.POST['msg_new']
  else:
    data['resolved_base'], data['msg_base'] = get_sha(
        data['base_tag'], data['tests_repo'])
    data['resolved_new'], data['msg_new'] = get_sha(
        data['new_tag'], data['tests_repo'])
    u = request.userdb.get_user(data['username'])
    if u.get('tests_repo', '') != data['tests_repo']:
      u['tests_repo'] = data['tests_repo']
      request.userdb.users.save(u)

  if len(data['resolved_base']) == 0 or len(data['resolved_new']) == 0:
    raise Exception('Unable to find branch!')

  # Check entered bench
  if data['base_tag'] == 'master':
    found = False
    api_url = data['tests_repo'].replace('https://github.com',
                                         'https://api.github.com/repos')
    api_url += '/commits'
    bs = re.compile(r'(^|\s)[Bb]ench[ :]+([0-9]{7})', re.MULTILINE)
    for c in requests.get(api_url).json():
      m = bs.search(c['commit']['message'])
      if m:
        found = True
        break
    if not found or m.group(2) != data['base_signature']:
      raise Exception('Bench signature of Base master does not match, '
                      + 'please "git pull upstream master" !')

  stop_rule = request.POST['stop_rule']

  # Check if the base branch of the test repo matches official master
  api_url = 'https://api.github.com/repos/official-stockfish/Stockfish'
  api_url += '/compare/master...' + data['resolved_base'][:7]
  master_diff = requests.get(api_url, headers={
    'Accept': 'application/vnd.github.v3.diff'
  })
  data['base_same_as_master'] = master_diff.text is ''

  # Integer parameters
  if stop_rule == 'sprt':
    data['sprt'] = fishtest.stats.stat_util.SPRT(alpha=0.05,
                                                 beta=0.05,
                                                 elo0=float(request.POST['sprt_elo0']),
                                                 elo1=float(request.POST['sprt_elo1']),
                                                 elo_model='logistic')
    # Limit on number of games played.
    # Shouldn't be hit in practice as long as it is larger than > ~200000
    # must scale with chunk_size to avoid overloading the server.
    data['num_games'] = 1000 * request.rundb.chunk_size
  elif stop_rule == 'spsa':
    data['num_games'] = int(request.POST['num-games'])
    if data['num_games'] <= 0:
      raise Exception('Number of games must be >= 0')

    data['spsa'] = {
      'A': int(request.POST['spsa_A']),
      'alpha': float(request.POST['spsa_alpha']),
      'gamma': float(request.POST['spsa_gamma']),
      'raw_params': request.POST['spsa_raw_params'],
      'iter': 0,
      'num_iter': int(data['num_games'] / 2),
      'clipping': request.POST['spsa_clipping'],
      'rounding': request.POST['spsa_rounding'],
    }
    data['spsa']['params'] = parse_spsa_params(
        request.POST['spsa_raw_params'], data['spsa'])
  else:
    data['num_games'] = int(request.POST['num-games'])
    if data['num_games'] <= 0:
      raise Exception('Number of games must be >= 0')

  max_games = 4000 * request.rundb.chunk_size
  if data['num_games'] > max_games:
    raise Exception('Number of games must be <= ' + str(max_games))

  data['threads'] = int(request.POST['threads'])
  data['priority'] = int(request.POST['priority'])
  data['throughput'] = int(request.POST['throughput'])

  if data['threads'] <= 0:
    raise Exception('Threads must be >= 1')

  return data


@view_config(route_name='tests_run', renderer='tests_run.mak', require_csrf=True)
def tests_run(request):
  if not authenticated_userid(request):
    request.session.flash('Please login')
    return HTTPFound(location=request.route_url('login'))
  if request.method == 'POST':
    try:
      data = validate_form(request)
      run_id = request.rundb.new_run(**data)
      request.actiondb.new_run(authenticated_userid(request),
                               request.rundb.get_run(run_id))
      cached_flash(request, 'Submitted test to the queue!')
      return HTTPFound(location='/tests/view/' + str(run_id))
    except Exception as e:
      request.session.flash(str(e))

  run_args = {}
  if 'id' in request.params:
    run_args = request.rundb.get_run(request.params['id'])['args']

  username = authenticated_userid(request)
  u = request.userdb.get_user(username)

  return {'args': run_args,
          'tests_repo': u.get('tests_repo', ''),
          'bench': get_master_bench()}


def can_modify_run(request, run):
  return (run['args']['username'] == authenticated_userid(request)
          or has_permission('approve_run', request.context, request))


@view_config(route_name='tests_modify', require_csrf=True, request_method='POST')
def tests_modify(request):
  if not authenticated_userid(request):
    request.session.flash('Please login')
    return HTTPFound(location=request.route_url('login'))
  if 'num-games' in request.POST:
    run = request.rundb.get_run(request.POST['run'])
    before = copy.deepcopy(run)

    if not can_modify_run(request, run):
      request.session.flash("Unable to modify another user's run!")
      return HTTPFound(location=request.route_url('tests'))

    existing_games = 0
    for chunk in run['tasks']:
      existing_games += chunk['num_games']
      if 'stats' in chunk:
        stats = chunk['stats']
        total = stats['wins'] + stats['losses'] + stats['draws']
        if total < chunk['num_games']:
          chunk['pending'] = True

    num_games = int(request.POST['num-games'])
    if (num_games > run['args']['num_games']
        and 'sprt' not in run['args']
        and 'spsa' not in run['args']):
      request.session.flash(
          'Unable to modify number of games in a fixed game test!')
      return HTTPFound(location=request.route_url('tests'))

    max_games = 4000 * request.rundb.chunk_size
    if num_games > max_games:
      request.session.flash('Number of games must be <= ' + str(max_games))
      return HTTPFound(location=request.route_url('tests'))

    if num_games > existing_games:
      # Create new chunks for the games
      new_chunks = request.rundb.generate_tasks(num_games - existing_games)
      run['tasks'] += new_chunks

    run['finished'] = False
    run['args']['num_games'] = num_games
    run['args']['priority'] = int(request.POST['priority'])
    run['args']['throughput'] = int(request.POST['throughput'])
    request.rundb.calc_itp(run)
    request.rundb.buffer(run, True)
    request.rundb.task_time = 0

    request.actiondb.modify_run(authenticated_userid(request), before, run)

    cached_flash(request, 'Run successfully modified!')
  return HTTPFound(location=request.route_url('tests'))


@view_config(route_name='tests_stop', require_csrf=True, request_method='POST')
def tests_stop(request):
  if not authenticated_userid(request):
    request.session.flash('Please login')
    return HTTPFound(location=request.route_url('login'))
  if 'run-id' in request.POST:
    run = request.rundb.get_run(request.POST['run-id'])
    if not can_modify_run(request, run):
      request.session.flash('Unable to modify another users run!')
      return HTTPFound(location=request.route_url('tests'))

    run['finished'] = True
    request.rundb.stop_run(request.POST['run-id'])
    request.actiondb.stop_run(authenticated_userid(request), run)
    cached_flash(request, 'Stopped run')
  return HTTPFound(location=request.route_url('tests'))


@view_config(route_name='tests_approve',
             require_csrf=True, request_method='POST')
def tests_approve(request):
  if not authenticated_userid(request):
    request.session.flash('Please login')
    return HTTPFound(location=request.route_url('login'))
  if not has_permission('approve_run', request.context, request):
    request.session.flash('Please login as approver')
    return HTTPFound(location=request.route_url('login'))
  username = authenticated_userid(request)
  run_id = request.POST['run-id']
  if request.rundb.approve_run(run_id, username):
    run = request.rundb.get_run(run_id)
    request.actiondb.approve_run(username, run)
    cached_flash(request, 'Approved run')
  else:
    request.session.flash('Unable to approve run!')
  return HTTPFound(location=request.route_url('tests'))


def purge_run(rundb, run):
  # Remove bad runs
  purged = False
  chi2 = calculate_residuals(run)
  if 'bad_tasks' not in run:
    run['bad_tasks'] = []
  for task in run['tasks']:
    if task['worker_key'] in chi2['bad_users']:
      purged = True
      task['bad'] = True
      run['bad_tasks'].append(task)
      run['tasks'].remove(task)

  if purged:
    # Generate new tasks if needed
    run['results_stale'] = True
    results = rundb.get_results(run)
    played_games = results['wins'] + results['losses'] + results['draws']
    if played_games < run['args']['num_games']:
      run['tasks'] += rundb.generate_tasks(
          run['args']['num_games'] - played_games)

    run['finished'] = False
    if 'sprt' in run['args'] and 'state' in run['args']['sprt']:
      fishtest.stats.stat_util.update_SPRT(results,run['args']['sprt'])
      run['args']['sprt']['state']=''

    rundb.buffer(run, True)

  return purged


@view_config(route_name='tests_purge', require_csrf=True, request_method='POST')
def tests_purge(request):
  if not has_permission('approve_run', request.context, request):
    request.session.flash('Please login as approver')
    return HTTPFound(location=request.route_url('login'))
  username = authenticated_userid(request)

  run = request.rundb.get_run(request.POST['run-id'])
  if not run['finished']:
    request.session.flash('Can only purge completed run')
    return HTTPFound(location=request.route_url('tests'))

  purged = purge_run(request.rundb, run)
  if not purged:
    request.session.flash('No bad workers!')
    return HTTPFound(location=request.route_url('tests'))

  request.actiondb.purge_run(username, run)

  cached_flash(request, 'Purged run')
  return HTTPFound(location=request.route_url('tests'))


@view_config(route_name='tests_delete', require_csrf=True, request_method='POST')
def tests_delete(request):
  if not authenticated_userid(request):
    request.session.flash('Please login')
    return HTTPFound(location=request.route_url('login'))
  if 'run-id' in request.POST:
    run = request.rundb.get_run(request.POST['run-id'])
    if not can_modify_run(request, run):
      request.session.flash('Unable to modify another users run!')
      return HTTPFound(location=request.route_url('tests'))

    run['deleted'] = True
    run['finished'] = True
    for w in run['tasks']:
      w['pending'] = False
    request.rundb.buffer(run, True)
    request.rundb.task_time = 0

    request.actiondb.delete_run(authenticated_userid(request), run)

    cached_flash(request, 'Deleted run')
  return HTTPFound(location=request.route_url('tests'))


def format_results(run_results, run):
  result = {'style': '', 'info': []}

  # win/loss/draw count
  WLD = [run_results['wins'], run_results['losses'], run_results['draws']]

  if 'spsa' in run['args']:
    result['info'].append('%d/%d iterations'
                          % (run['args']['spsa']['iter'],
                             run['args']['spsa']['num_iter']))
    result['info'].append('%d/%d games played'
                          % (WLD[0] + WLD[1] + WLD[2],
                             run['args']['num_games']))
    return result

  # If the score is 0% or 100% the formulas will crash
  # anyway the statistics are only asymptotic
  if WLD[0] == 0 or WLD[1] == 0:
    result['info'].append('Pending...')
    return result

  state = 'unknown'
  if 'sprt' in run['args']:
    sprt = run['args']['sprt']
    state = sprt.get('state', '')
    elo_model = sprt.get('elo_model', 'BayesElo')
    if not 'llr' in sprt:  # legacy
      fishtest.stats.stat_util.update_SPRT(run_results,sprt)
    if elo_model == 'BayesElo':
      result['info'].append('LLR: %.2f (%.2lf,%.2lf) [%.2f,%.2f]'
                            % (sprt['llr'],
                               sprt['lower_bound'], sprt['upper_bound'],
                               sprt['elo0'], sprt['elo1']))
    else:
      result['info'].append('LLR: %.2f (%.2lf,%.2lf) {%.2f,%.2f}'
                            % (sprt['llr'],
                               sprt['lower_bound'], sprt['upper_bound'],
                               sprt['elo0'], sprt['elo1']))
  else:
    if 'pentanomial' in run_results.keys():
      elo, elo95, los = fishtest.stats.stat_util.get_elo(
          run_results['pentanomial'])
    else:
      elo, elo95, los = fishtest.stats.stat_util.get_elo(
          [WLD[1], WLD[2], WLD[0]])

    # Display the results
    eloInfo = 'ELO: %.2f +-%.1f (95%%)' % (elo, elo95)
    losInfo = 'LOS: %.1f%%' % (los * 100)

    result['info'].append(eloInfo + ' ' + losInfo)

    if los < 0.05:
      state = 'rejected'
    elif los > 0.95:
      state = 'accepted'

  result['info'].append('Total: %d W: %d L: %d D: %d'
                        % (sum(WLD), WLD[0], WLD[1], WLD[2]))
  if 'pentanomial' in run_results.keys():
    result['info'].append("Ptnml(0-2): " + ", ".join(
        str(run_results['pentanomial'][i]) for i in range(0, 5)))

  if state == 'rejected':
    if WLD[0] > WLD[1]:
      result['style'] = 'yellow'
    else:
      result['style'] = '#FF6A6A'
  elif state == 'accepted':
    if ('sprt' in run['args']
        and (float(sprt['elo0']) + float(sprt['elo1'])) < 0.0):
      result['style'] = '#66CCFF'
    else:
      result['style'] = '#44EB44'
  return result


UUID_MAP = defaultdict(dict)
key_lock = threading.Lock()


def get_worker_key(task):
  global UUID_MAP

  if 'worker_info' not in task:
    return '-'
  username = task['worker_info'].get('username', '')
  cores = str(task['worker_info']['concurrency'])

  uuid = task['worker_info'].get('unique_key', '')
  with key_lock:
    if uuid not in UUID_MAP[username]:
      next_idx = len(UUID_MAP[username])
      UUID_MAP[username][uuid] = next_idx

  worker_key = '%s-%scores' % (username, cores)
  suffix = UUID_MAP[username][uuid]
  if suffix != 0:
    worker_key += "-" + str(suffix)

  return worker_key


def get_chi2(tasks, bad_users):
  """Perform chi^2 test on the stats from each worker"""
  results = {'chi2': 0.0, 'dof': 0, 'p': 0.0, 'residual': {}}

  # Aggregate results by worker
  users = {}
  for task in tasks:
    task['worker_key'] = get_worker_key(task)
    if 'worker_info' not in task:
      continue
    key = get_worker_key(task)
    if key in bad_users:
      continue
    stats = task.get('stats', {})
    wld = [float(stats.get('wins', 0)),
           float(stats.get('losses', 0)), float(stats.get('draws', 0))]
    if wld == [0.0, 0.0, 0.0]:
      continue
    if key in users:
      for idx in range(len(wld)):
        users[key][idx] += wld[idx]
    else:
      users[key] = wld

  if len(users) == 0:
    return results

  observed = numpy.array(list(users.values()))
  rows, columns = observed.shape
  # Results only from one worker: skip the test for workers homogeneity
  if rows == 1:
    results = {'chi2': float('nan'), 'dof': 0,
               'p': float('nan'), 'residual': {}}
    return results

  column_sums = numpy.sum(observed, axis=0)
  columns_not_zero = sum(i > 0 for i in column_sums)
  df = (rows - 1) * (columns - 1)

  if columns_not_zero == 0:
    return results
  # Results only of one type: workers are identical wrt the test
  elif columns_not_zero == 1:
    results = {'chi2': 0.0, 'dof': df, 'p': 1.0, 'residual': {}}
    return results
  # Results only of two types: workers are identical wrt the missing result type
  # Change the data shape to avoid divide by zero
  elif columns_not_zero == 2:
    idx = numpy.argwhere(numpy.all(observed[..., :] == 0, axis=0))
    observed = numpy.delete(observed, idx, axis=1)
    column_sums = numpy.sum(observed, axis=0)

  row_sums = numpy.sum(observed, axis=1)
  grand_total = numpy.sum(column_sums)

  expected = numpy.outer(row_sums, column_sums) / grand_total
  raw_residual = observed - expected
  std_error = numpy.sqrt(expected *
                         numpy.outer((1 - row_sums / grand_total),
                                     (1 - column_sums / grand_total)))
  adj_residual = raw_residual / std_error
  for idx in range(len(users)):
    users[list(users.keys())[idx]] = numpy.max(numpy.abs(adj_residual[idx]))
  chi2 = numpy.sum(raw_residual * raw_residual / expected)
  return {
    'chi2': chi2,
    'dof': df,
    'p': 1 - scipy.stats.chi2.cdf(chi2, df),
    'residual': users,
  }


def calculate_residuals(run):
  bad_users = set()
  chi2 = get_chi2(run['tasks'], bad_users)
  residuals = chi2['residual']

  # Limit bad users to 1 for now
  for _ in range(1):
    worst_user = {}
    for task in run['tasks']:
      if task['worker_key'] in bad_users:
        continue
      task['residual'] = residuals.get(task['worker_key'], 0.0)

      # Special case crashes or time losses
      stats = task.get('stats', {})
      crashes = stats.get('crashes', 0)
      if crashes > 3:
        task['residual'] = 8.0

      if abs(task['residual']) < 2.0:
        task['residual_color'] = '#44EB44'
      elif abs(task['residual']) < 2.7:
        task['residual_color'] = 'yellow'
      else:
        task['residual_color'] = '#FF6A6A'

      if chi2['p'] < 0.001 or task['residual'] > 7.0:
        if len(worst_user) == 0 or task['residual'] > worst_user['residual']:
          worst_user['worker_key'] = task['worker_key']
          worst_user['residual'] = task['residual']

    if len(worst_user) == 0:
      break
    bad_users.add(worst_user['worker_key'])
    residuals = get_chi2(run['tasks'], bad_users)['residual']

  chi2['bad_users'] = bad_users
  return chi2


@view_config(route_name='tests_stats', renderer='tests_stats.mak')
def tests_stats(request):
  run = request.rundb.get_run(request.matchdict['id'])
  request.rundb.get_results(run)
  return {'run': run}


@view_config(route_name='tests_machines', renderer='machines_table.mak')
def tests_machines(request):
  machines = request.rundb.get_machines()
  for machine in machines:
    machine['last_updated'] = delta_date(machine['last_updated'])
  return {
    'machines': machines
  }


@view_config(route_name='tests_view_spsa_history', renderer='json')
def tests_view_spsa_history(request):
  run = request.rundb.get_run(request.matchdict['id'])
  if 'spsa' not in run['args']:
    return {}

  return run['args']['spsa']


@view_config(route_name='tests_view', renderer='tests_view.mak')
def tests_view(request):
  run = request.rundb.get_run(request.matchdict['id'])
  if run is None:
    raise exception_response(404)
  results = request.rundb.get_results(run)
  run['results_info'] = format_results(results, run)
  run_args = [('id', str(run['_id']), '')]

  for name in ['new_tag', 'new_signature', 'new_options', 'resolved_new',
               'base_tag', 'base_signature', 'base_options', 'resolved_base',
               'sprt', 'num_games', 'spsa', 'tc', 'threads', 'book',
               'book_depth', 'auto_purge', 'priority', 'itp', 'username',
               'tests_repo', 'info']:

    if name not in run['args']:
      continue

    value = run['args'][name]
    url = ''

    if name == 'new_tag' and 'msg_new' in run['args']:
      value += '  (' + run['args']['msg_new'][:50] + ')'

    if name == 'base_tag' and 'msg_base' in run['args']:
      value += '  (' + run['args']['msg_base'][:50] + ')'

    if name == 'sprt' and value != '-':
      value = 'elo0: %.2f alpha: %.2f elo1: %.2f beta: %.2f state: %s (%s)' % \
              (value['elo0'], value['alpha'], value['elo1'], value['beta'],
               value.get('state', '-'), value.get('elo_model', 'BayesElo'))

    if name == 'spsa' and value != '-':
      iter_local = value['iter'] + 1  # assume at least one completed,
                                      # and avoid division by zero
      params = ['param: %s, best: %.2f, start: %.2f, min: %.2f, max: %.2f, c %f, a %f' %
                (p['name'], p['theta'], p['start'], p['min'], p['max'],
                 p['c'] / (iter_local ** value['gamma']),
                 p['a'] / (value['A'] + iter_local) ** value['alpha'])
                for p in value['params']]
      value = 'Iter: %d, A: %d, alpha %f, gamma %f, clipping %s, rounding %s\n%s' \
              % (iter_local, value['A'], value['alpha'], value['gamma'],
                 value['clipping'] if 'clipping' in value else 'old',
                 value['rounding'] if 'rounding' in value else 'deterministic',
                 '\n'.join(params))

    if 'tests_repo' in run['args']:
      if name == 'new_tag':
        url = run['args']['tests_repo'] + '/commit/' + run['args']['resolved_new']
      elif name == 'base_tag':
        url = run['args']['tests_repo'] + '/commit/' + run['args']['resolved_base']
      elif name == 'tests_repo':
        url = value

    try:
      strval = str(value)
    except:
      strval = value.encode('ascii', 'replace')
    strval = html.escape(strval)
    run_args.append((name, strval, url))

  active = 0
  cores = 0
  for task in run['tasks']:
    if task['active']:
      active += 1
      cores += task['worker_info']['concurrency']
    last_updated = task.get('last_updated', datetime.datetime.min)
    task['last_updated'] = last_updated

  return {'run': run, 'run_args': run_args, 'chi2': calculate_residuals(run),
          'approver': has_permission('approve_run', request.context, request),
          'totals': '(%s active worker%s with %s core%s)'
          % (active, ('s' if active != 1 else ''),
             cores, ('s' if cores != 1 else ''))}


def post_result(run):
  title = run['args']['new_tag'][:23]

  if 'username' in run['args']:
    title += '  (' + run['args']['username'] + ')'

  body = FISH_URL + '%s\n\n' % (str(run['_id']))

  body += run['start_time'].strftime("%d-%m-%y") + ' from '
  body += run['args'].get('username', '') + '\n\n'

  body += run['args']['new_tag'] + ': ' + run['args'].get(
      'msg_new', '') + '\n'
  body += run['args']['base_tag'] + ': ' + run['args'].get(
      'msg_base', '') + '\n\n'

  body += 'TC: ' + run['args']['tc'] + ' th ' + str(
      run['args'].get('threads', 1)) + '\n'
  body += '\n'.join(run['results_info']['info']) + '\n\n'

  body += run['args'].get('info', '') + '\n\n'

  msg = MIMEText(body)
  msg['Subject'] = title
  msg['From'] = 'fishtest@noreply.github.com'
  msg['To'] = 'fishcooking_results@googlegroups.com'

  s = smtplib.SMTP('localhost')
  s.sendmail(msg['From'], [msg['To']], msg.as_string())
  s.quit()


def get_paginated_finished_runs(request):
  username = request.matchdict.get('username', '')
  success_only = request.params.get('success_only', False)
  yellow_only = request.params.get('yellow_only', False)
  ltc_only = request.params.get('ltc_only', False)

  page_idx = max(0, int(request.params.get('page', 1)) - 1)
  page_size = 50
  finished_runs, num_finished_runs = request.rundb.get_finished_runs(
    username=username, success_only=success_only,
    yellow_only=yellow_only, ltc_only=ltc_only,
    skip=page_idx * page_size, limit=page_size)

  pages = [{'idx': 'Prev', 'url': '?page={}'.format(page_idx),
            'state': 'disabled' if page_idx == 0 else ''}]
  for idx, _ in enumerate(range(0, num_finished_runs, page_size)):
    if idx < 5 or abs(page_idx - idx) < 5 or idx > (num_finished_runs / page_size) - 5:
      pages.append({'idx': idx + 1, 'url': '?page={}'.format(idx + 1),
                    'state': 'active' if page_idx == idx else ''})
    elif pages[-1]['idx'] != '...':
      pages.append({'idx': '...', 'url': '', 'state': 'disabled'})
  pages.append({'idx': 'Next', 'url': '?page={}'.format(page_idx + 2),
                'state': 'disabled' if page_idx + 1 == len(pages) - 1 else ''})

  for page in pages:
    if success_only:
      page['url'] += '&success_only=1'
    if yellow_only:
      page['url'] += '&yellow_only=1'
    if ltc_only:
      page['url'] += '&ltc_only=1'

  failed_runs = []
  for run in finished_runs:
    # Ensure finished runs have results_info
    # TODO do this when the run finishes, not when it's viewed
    results = request.rundb.get_results(run)
    if 'results_info' not in run:
      run['results_info'] = format_results(results, run)

    # Look for failed runs
    if results['wins'] + results['losses'] + results['draws'] == 0:
      failed_runs.append(run)

  return {
    'finished_runs': finished_runs,
    'finished_runs_pages': pages,
    'num_finished_runs': num_finished_runs,
    'failed_runs': failed_runs,
    'page_idx': page_idx,
  }


@view_config(route_name='tests_finished', renderer='tests_finished.mak')
def tests_finished(request):
  return get_paginated_finished_runs(request)


def remaining_hours(run):
  r = run['results']
  if 'sprt' in run['args']:
    # current average number of games. Regularly update / have server guess?
    expected_games = 53000
    # checking randomly, half the expected games needs still to be done
    remaining_games = expected_games / 2
  else:
    expected_games = run['args']['num_games']
    remaining_games = max(0,
                          expected_games
                          - r['wins'] - r['losses'] - r['draws'])
  game_secs = parse_tc(run['args']['tc'])
  return game_secs * remaining_games * int(
      run['args'].get('threads', 1)) / (60*60)


def aggregate_and_update_unfinished_runs(unfinished_runs, rundb):
  runs = {'pending': [], 'failed': [], 'active': []}
  for run in unfinished_runs:
    results = rundb.get_results(run, False)
    run['results_info'] = format_results(results, run)
    state = 'finished'
    for task in run['tasks']:
      if task['active']:
        state = 'active'
      elif task['pending'] and not state == 'active':
        state = 'pending'
    if state == 'finished':
      # Auto-purge runs here (this is hacky, ideally we would do it
      # when the run was finished, not when it is first viewed)
      # TODO auto-purge when a run finishes
      purged = 0
      if (run['args'].get('auto_purge', True)
          and 'spsa' not in run['args'] and run['args']['threads'] == 1):
        while purge_run(rundb, run) and purged < 5:
          purged += 1
          run = rundb.get_run(run['_id'])
          results = rundb.get_results(run, True)
          run['results_info'] = format_results(results, run)
          rundb.buffer(run, True)
      if purged == 0:
        # The run is finished and will no longer be updated after this
        run['finished'] = True
        # Decouple the styling of the run from its finished status
        if run['results_info']['style'] == '#44EB44':
          run['is_green'] = True
        elif run['results_info']['style'] == 'yellow':
          run['is_yellow'] = True
        rundb.buffer(run, True)
        post_result(run)
    else:
      runs[state].append(run)
  runs['pending'].sort(key=lambda run: (run['args']['priority'],
                                        run['args']['itp']
                                        if 'itp' in run['args'] else 100))
  runs['active'].sort(reverse=True, key=lambda run: (
      'sprt' in run['args'],
      run['args'].get('sprt',{}).get('llr',0),
      'spsa' not in run['args'],
      run['results']['wins'] + run['results']['draws']
      + run['results']['losses']))

  # Update results_info on runs using machine info
  machines = rundb.get_machines()
  cores = sum([int(m['concurrency']) for m in machines])
  nps = sum([int(m['concurrency']) * m['nps'] for m in machines])
  pending_hours = 0
  for run in runs['pending'] + runs['active']:
    if cores > 0:
      eta = remaining_hours(run) / cores
      pending_hours += eta
    info = run['results_info']
    if 'Pending...' in info['info']:
      if cores > 0:
        info['info'][0] += ' (%.1f hrs)' % (eta)
      if 'sprt' in run['args']:
        sprt = run['args']['sprt']
        elo_model = sprt.get('elo_model', 'BayesElo')
        if elo_model == 'BayesElo':
          info['info'].append(('[%.2f,%.2f]')
                              % (sprt['elo0'], sprt['elo1']))
        else:
          info['info'].append(('{%.2f,%.2f}')
                              % (sprt['elo0'], sprt['elo1']))
  return (runs, pending_hours, cores, nps)


@view_config(route_name='tests_user', renderer='tests_user.mak')
def tests_user(request):
  username = request.matchdict.get('username', '')
  result = {
    **get_paginated_finished_runs(request),
    'username': username
  }
  if int(request.params.get('page', 1)) > 1:
    # page 2 and beyond only show finished test results
    return result
  unfinished_runs = [
    r for r in request.rundb.get_unfinished_runs() if r['args'].get('username') == username
  ]
  result['runs'] = aggregate_and_update_unfinished_runs(unfinished_runs, request.rundb)[0]
  return result


def homepage_results(request):
  # Calculate games_per_minute from current machines
  games_per_minute = 0.0
  machines = request.rundb.get_machines()
  for machine in machines:
    machine['last_updated'] = delta_date(machine['last_updated'])
    if machine['nps'] != 0:
      games_per_minute += (
          (machine['nps'] / 1200000.0)
          * (60.0 / parse_tc(machine['run']['args']['tc']))
          * int(machine['concurrency']))
  machines.reverse()

  # Update unfinished_runs + fetch paginated finished_runs
  unfinished_runs = request.rundb.get_unfinished_runs()
  (runs, pending_hours, cores, nps) = aggregate_and_update_unfinished_runs(
    unfinished_runs, request.rundb)

  return {
    **get_paginated_finished_runs(request),
    'runs': runs,
    'machines': machines,
    'pending_hours': '%.1f' % (pending_hours),
    'cores': cores,
    'nps': nps,
    'games_per_minute': int(games_per_minute),
  }


# For caching the homepage tests output
cache_time = 2
last_tests = None
last_time = 0

# Guard against parallel builds of main page
building = threading.Semaphore()

@view_config(route_name='tests', renderer='tests.mak')
def tests(request):
  if int(request.params.get('page', 1)) > 1:
    # page 2 and beyond only show finished test results
    return get_paginated_finished_runs(request)

  global last_tests, last_time
  if time.time() - last_time > cache_time:
    acquired = building.acquire(last_tests is None)
    if not acquired:
      # We have a current cache and another thread is rebuilding,
      # so return the current cache
      pass
    elif time.time() - last_time < cache_time:
      # Another thread has built the cache for us, so we are done
      building.release()
    else:
      # Not cached, so calculate and fetch homepage results
      try:
        last_tests = homepage_results(request)
      except Exception as e:
        print('Overview exception: ' + str(e))
        if not last_tests:
          raise e
      finally:
        last_time = time.time()
        building.release()
  return {
    **last_tests,
    'machines_shown': request.cookies.get('machines_state') == 'Hide',
    'pending_shown': request.cookies.get('pending_state') == 'Hide'
  }
