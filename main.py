import flask
from flask_sqlalchemy import SQLAlchemy

from dateutil import parser as dateparser
import untangle

import json
import tweepy
import yaml

from arxiv_regex import get_arxiv_id

'''
Issue: quoting a tweet won't include it
i.e.
> My new work on RNNs: arxiv.org/...
> John's new work on RNNs! twitter.org/linkabove/...
'''

config = yaml.load(open('config.yaml'))

# Set up the Twitter API
auth = tweepy.OAuthHandler(config['consumer_key'], config['consumer_secret'])
auth.secure = True
auth.set_access_token(config['access_token'], config['access_token_secret'])
api = tweepy.API(auth)

# Flask app
app = flask.Flask(__name__)
app.secret_key = config['app_secret_key']
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///tweet.db'
db = SQLAlchemy(app)

# Database

retweets = db.Table('retweets',
  db.Column('tweet_id', db.Integer, db.ForeignKey('tweet.id')),
  db.Column('user_id', db.Integer, db.ForeignKey('user.id')),
  db.UniqueConstraint('tweet_id', 'user_id'),
)

papertweets = db.Table('papertweets',
  db.Column('tweet_id', db.Integer, db.ForeignKey('tweet.id')),
  db.Column('paper_id', db.Integer, db.ForeignKey('paper.id')),
  db.UniqueConstraint('tweet_id', 'paper_id'),
)

class User(db.Model):
  id = db.Column(db.Integer, primary_key=True)
  screen_name = db.Column(db.String(256), unique=True)
  json = db.Column(db.Text)

  tweets = db.relationship('Tweet', backref='author', lazy='dynamic')

  def __init__(self, id, screen_name, name):
    self.id = id
    self.screen_name = screen_name.lower()
    self.name = name

  def __repr__(self):
    return '<User {} - {}>'.format(self.id, self.screen_name)

class Tweet(db.Model):
  id = db.Column(db.Integer, primary_key=True)
  json = db.Column(db.Text)
  is_retweet = db.Column(db.Boolean, default=False)

  author_id = db.Column(db.Integer, db.ForeignKey('user.id'))

  retweets = db.relationship('User', secondary=retweets, backref=db.backref('retweets', lazy='dynamic'))

  def __init__(self, id, json):
    self.id = id
    self.json = json

  def link(self):
    return 'https://twitter.com/{}/status/{}'.format(self.author.screen_name, self.id)

  def __repr__(self):
    return '<Tweet {}>'.format(self.id)

class Paper(db.Model):
  id = db.Column(db.Integer, primary_key=True)
  arxiv_id = db.Column(db.String(256), unique=True)
  title = db.Column(db.String(256))
  summary = db.Column(db.Text)
  published = db.Column(db.DateTime)
  authors = db.Column(db.Text)

  tweets = db.relationship('Tweet', secondary=papertweets, backref=db.backref('papers', lazy='dynamic'))

  def __init__(self, arxiv_id):
    self.arxiv_id = arxiv_id

  def link(self, section='abs'):
    return 'http://arxiv.org/{}/{}'.format(section, self.arxiv_id)

  def update(self):
    url = 'http://export.arxiv.org/api/query?id_list={}&max_results=10'.format(self.arxiv_id)
    paper = untangle.parse(url)
    self.title = paper.feed.entry.title.cdata
    # Mixing times with and without timezones causes issues -_-
    self.published = dateparser.parse(paper.feed.entry.published.cdata).replace(tzinfo=None)
    self.summary = paper.feed.entry.summary.cdata
    self.authors = ', '.join(x.name.cdata for x in paper.feed.entry.author)

  def __repr__(self):
    return '<Paper {}>'.format(self.arxiv_id)

# Helpers

def tweet_has_url(t, url):
  return 'urls' in t.entities and any(u for u in t.entities['urls'] if url in u['expanded_url'].lower())

def add_tweet(tweet):
  print [url['expanded_url'] for url in tweet.entities['urls']]
  papers = [get_arxiv_id(url['expanded_url']) for url in tweet.entities['urls'] if get_arxiv_id(url['expanded_url'])]
  if not papers:
    return
  # If a retweet, add the original tweet first
  original_tweet = None
  if getattr(tweet, 'retweeted_status', None):
    otweet = getattr(tweet, 'retweeted_status', None)
    original_tweet = Tweet.query.filter_by(id=otweet.id).first()
    if original_tweet is None:
      original_tweet = add_tweet(otweet)
  ##
  author = User.query.filter_by(id=tweet.user.id).first()
  if author is None:
    author = User(tweet.user.id, tweet.user.screen_name, json.dumps(tweet.user._json))
  ## 
  t = Tweet.query.filter_by(id=tweet.id).first()
  if t is None:
    t = Tweet(tweet.id, json.dumps(tweet._json))
    t.author = author
    t.is_retweet = True if original_tweet else False
  ##
  for paper_id in papers:
    p = Paper.query.filter_by(arxiv_id=paper_id).first()
    if p is None:
      p = Paper(paper_id)
      p.update()
    if t not in p.tweets:
      with db.session.no_autoflush:
        t.papers.append(p)
    db.session.add(p)
  ##
  db.session.add(author)
  db.session.add(t)
  if original_tweet:
    if author not in original_tweet.retweets:
      with db.session.no_autoflush:
        original_tweet.retweets.append(author)
    db.session.add(original_tweet)
  db.session.commit()
  return t

# Views

@app.route('/fetch_timeline/<username>')
def fetch_timeline(username):
  results = api.user_timeline(screen_name=username, count=200, page=0)
  tweets = [add_tweet(tweet) for tweet in results]
  total_processed = sum(1 if t else 0 for t in tweets)
  #flask.flash('Processed {} tweets with papers from the timeline of {}'.format(total_processed, username))
  return flask.redirect(flask.url_for('show_all'))

@app.route('/fetch_search/<username>')
def fetch_search(username):
  results = api.search('@{} arxiv.org'.format(username), count=200)
  tweets = [add_tweet(tweet) for tweet in results]
  total_processed = sum(1 if t else 0 for t in tweets)
  #flask.flash('Processed {} tweets with papers from the search of {}'.format(total_processed, username))
  return flask.redirect(flask.url_for('show_all'))

@app.route('/refresh')
def refresh():
  to_follow = set(config['to_follow'].split())
  old_count = Paper.query.count()
  for user in to_follow:
    fetch_timeline(user)
    if config.get('fetch_search', False):
      fetch_search(user)
  updated_count = Paper.query.count()
  # Note how many new papers were added in this refresh
  if old_count != updated_count:
    diff = updated_count - old_count
    flask.flash('Added {} new paper{}'.format(diff, 's' if diff > 1 else ''))
  else:
    flask.flash('No new papers were found')
  return flask.redirect(flask.url_for('show_all'))

@app.route('/rate_limits')
def rate_limits():
  rates = api.rate_limit_status()
  return flask.render_template('show_rates.html', rates=rates)

@app.route('/tweets')
@app.route('/tweets/<int:page>')
def show_tweets(page=1):
  return flask.render_template('show_tweets.html', tweets=Tweet.query.filter_by(is_retweet=False).order_by(Tweet.id.desc()).paginate(page=page, per_page=config.get('per_page', 20), error_out=False))

@app.route('/')
@app.route('/<int:page>')
def show_all(page=1):
  return flask.render_template('show_all.html', papers=Paper.query.order_by(Paper.published.desc()).paginate(page=page, per_page=config.get('per_page', 20), error_out=False))

if __name__ == '__main__':
  app.run()
