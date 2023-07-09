import logging
import os
import re
import sys
import time
from hashlib import md5
from typing import Optional

import feedparser
import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from bs4 import BeautifulSoup
from peewee import SqliteDatabase, Model, TextField, IntegerField
from requests import RequestException

BOT_TOKEN = os.environ['BOT_TOKEN']
TELEGRAM_API_URL = f'https://api.telegram.org/bot{BOT_TOKEN}'
CHANNEL_ID = -1001626800013

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s - %(message)s',
                    datefmt='%Y-%m-%dT%H:%M:%S%z', stream=sys.stdout)

logger = logging.getLogger(__name__)

db = SqliteDatabase('ildolomiti.db')


class Article(Model):
    post_id = IntegerField(null=True)
    title = TextField()
    link = TextField()
    published = IntegerField()
    telegram_message_id = IntegerField(null=True)

    class Meta:
        database = db


def check():
    logger.info('Checking...')

    feed = feedparser.parse('https://www.ildolomiti.it/rss.xml?_=' + str(int(time.time())))

    if Article.select().count() == 0:
        logger.info('First run, populating database...')
        for entry in reversed(feed.entries):
            article = Article(
                post_id=None,
                title=entry.title,
                link=entry.link,
                published=int(time.mktime(entry.published_parsed)),
                telegram_message_id=None
            )
            article.save()
        logger.info('Done!')
        return

    for entry in reversed(feed.entries):
        article = Article.get_or_none(link=entry.link)
        if article:
            continue
        else:
            process_new_article(entry)

    logger.info('Done!')


def update_article(article: Article, entry):
    logger.info(f'Updating article: {entry.link}')

    article.title = entry.title
    article.save()


def process_new_article(entry):
    category = re.match(r'https://www\.ildolomiti\.it/(\w+)/', entry.link)
    category = category.group(1) if category else None
    if category == 'blog' or category == 'necrologi' or category == 'video':
        return

    details = fetch_article_details(entry.link)

    message = {
        'title': entry.title.strip(),
        'link': entry.link,
        'category': category,
        'description': details['description'] or entry.description,
        'image': details['image'] or 'fallback.jpg',
    }

    # Title is different, since we could match the post by post ID but couldn't by link/title
    if details['post_id'] and (article := Article.get_or_none(post_id=details['post_id'])):
        logger.info(f'Updating article: {entry.link} (old: {article.link})')
        if not article.telegram_message_id:
            logger.error('Article has no telegram_message_id, skipping')
        try:
            send_message(message, article.telegram_message_id)
        except RequestException:
            logger.exception('Error updating message')
            return  # so that it's retried later
        send_log(article, entry)
        article.title = message['title']
        article.link = message['link']
        article.save()
    # Otherwise assume that it's new
    else:
        logger.info(f'Sending article: {entry.link}')
        try:
            message_id = send_message(message)
        except RequestException:
            logger.exception('Error sending message')
            return  # so that it's retried later
        Article.create(
            post_id=details['post_id'],
            title=message['title'],
            link=message['link'],
            published=time.mktime(entry.published_parsed),
            telegram_message_id=message_id
        )


def fetch_article_details(link: str) -> dict:
    # TODO: catch
    resp = requests.get(
        link + '?_=' + str(int(time.time())),  # fix for 404 ending up in the dolomiti cache
        headers={
            'User-Agent': 'Il Dolomiti Telegram (+https://github.com/matteocontrini/ildolomiti-telegram)'
        },
        timeout=10
    )

    soup = BeautifulSoup(resp.text, 'html.parser')

    post_id = None
    description = None
    image = None

    article = soup.select_one('article[id^="node-"]')
    if article:
        post_id = article['id'].split('-')[1]
        description = article.find('div', class_='artSub')
        if description:
            description = description.text.strip()
        else:
            logger.error('Description not found')
        image_url = soup.find('meta', property='og:image')
        if image_url:
            image_url = image_url['content']
            image = download_image(image_url)
        else:
            logger.error('Image meta tag not found')
    else:
        logger.error('Article node not found')

    return {
        'post_id': post_id,
        'description': description,
        'image': image,
    }


def download_image(image_url: str) -> Optional[str]:
    if not image_url:
        return None
    try:
        resp = requests.get(image_url, timeout=10)
        resp.raise_for_status()
        filename = 'images/' + md5(image_url.encode('utf-8')).hexdigest()
        with open(filename, 'wb') as f:
            f.write(resp.content)
        return filename
    except (Exception,):
        logger.exception('Error downloading image')
        return None


def send_message(message, telegram_message_id=None) -> int:
    msg = ''
    if message['category']:
        msg += f'#{message["category"]} — '

    msg += f'<strong>{telegram_escape(message["title"])}</strong>'

    if message['description']:
        msg += f'\n\n<i>{telegram_escape(message["description"])}</i>'

    msg += f'\n\n📰 <a href="{message["link"]}">Leggi articolo</a>'

    if telegram_message_id:
        payload = {
            'chat_id': CHANNEL_ID,
            'message_id': telegram_message_id,
            'caption': msg,
            'parse_mode': 'HTML',
        }
        resp = requests.post(f'{TELEGRAM_API_URL}/editMessageCaption', json=payload)
    else:
        payload = {
            'chat_id': CHANNEL_ID,
            'caption': msg,
            'parse_mode': 'HTML',
        }
        resp = requests.post(f'{TELEGRAM_API_URL}/sendPhoto',
                             data=payload,
                             files={
                                 'photo': open(message['image'], 'rb')
                             })

    # Error while editing
    if resp.status_code != 200 and telegram_message_id:
        # Ignore 'message is not modified' error
        if resp.status_code == 400 and 'message is not modified' in resp.text:
            return 0

        # Log but don't raise
        logger.error(f'Error editing message: {resp.text}')
        return 0
    # Error while sending
    elif resp.status_code != 200:
        logger.error(f'Error sending message: {resp.text}')
        resp.raise_for_status()

    return resp.json()['result']['message_id']


def send_log(article: Article, entry):
    try:
        requests.post(f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage', json={
            'chat_id': CHANNEL_ID,
            'text': f'<code>{telegram_escape(article.title)}</code>\n\n'
                    f'<code>{telegram_escape(entry.title)}</code>\n\n'
                    f'<code>{telegram_escape(article.link)}</code>\n\n'
                    f'<code>{telegram_escape(entry.link)}</code>',
            'parse_mode': 'HTML',
        })
    except (Exception,):
        logger.exception('Error sending log')


def telegram_escape(text: str) -> str:
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def clean():
    logger.info('Cleaning old articles')
    # Keep the last 200 articles
    Article.delete().where(Article.id.not_in(
        Article.select(Article.id).order_by(Article.id.desc()).limit(200)
    )).execute()

    logger.info('Cleaning old images')
    for filename in os.listdir('images'):
        os.remove(os.path.join('images', filename))


if __name__ == '__main__':
    db.create_tables([Article])
    os.makedirs('images', exist_ok=True)

    clean()
    check()

    scheduler = BlockingScheduler()
    scheduler.add_job(check, trigger=CronTrigger(minute='*/10'))
    scheduler.add_job(clean, trigger=CronTrigger(minute='5', hour='1'))

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
