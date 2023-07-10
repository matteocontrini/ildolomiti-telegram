# Il Dolomiti Telegram

This repository contains the robot that powers the [@ildolomitinews](https://t.me/ildolomitinews) Telegram channel.

Improvements over the [previous version](https://github.com/matteocontrini/TelegramFeeder):

- Duplicate article detection based on Drupal's node ID
  - When the title of an article changes, the already sent Telegram message is modified
- Retry at next round when an article fetch fails
- Download images and upload them "manually" to Telegram API to avoid fetch failures
  - Fallback to a placeholder image if the image couldn't be downloaded
- Cache busting on article URLs, to avoid incurring into 404. Previously, if you requested an article to soon it would 404 and stay 404 in the edge cache for that particular request
- Improved tag parsing
