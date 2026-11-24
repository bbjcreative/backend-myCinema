# Version: 10.0 (AWS Lambda & S3 Migration)
# -*- coding: utf-8 -*-
"""
This script scrapes movie data from cinema.com.my. It is refactored for serverless
execution on AWS Lambda. It downloads poster images and uploads them to an S3
bucket, and saves the final aggregated movie data as a JSON file in the same bucket.
"""

import httpx
import json
import os
import re
import asyncio
import traceback
import boto3
from botocore.exceptions import ClientError
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# --- AWS & Scraper Configuration ---
# S3_BUCKET_NAME is retrieved from Lambda environment variables
S3_BUCKET_NAME = os.environ.get('S3_BUCKET_NAME')
AWS_REGION = os.environ.get('AWS_REGION', 'ap-southeast-1')

# --- Scrape Settings ---
MAX_MOVIES_TO_SCRAPE = None # Set to None to scrape all movies
MAX_DAYS_TO_SCRAPE = 5      # Scrape a full week
REQUEST_DELAY = 2           # Seconds to wait between most requests

# --- URL & Headers ---
MOVIES_NOWSHOWING_URL = 'https://www.cinema.com.my/movies/nowshowing.aspx'
BASE_URL = 'https://www.cinema.com.my'
HTTP_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://www.cinema.com.my/'
}

# --- AWS S3 Client ---
s3_client = boto3.client('s3', region_name=AWS_REGION)

def get_malaysian_time() -> datetime:
    """Returns the current time in Malaysian timezone (GMT+8)."""
    return datetime.utcnow() + timedelta(hours=8)

async def download_and_upload_poster_to_s3(client: httpx.AsyncClient, image_url: str) -> str | None:
    """
    Downloads an image to the /tmp/ directory and uploads it to S3.
    Returns the S3 path of the uploaded image.
    """
    if not image_url or image_url == 'N/A':
        return None

    filename = os.path.basename(image_url)
    local_temp_path = os.path.join('/tmp', filename)
    
    current_time = get_malaysian_time()
    s3_key = f"posters/{current_time.strftime('%Y')}/{current_time.strftime('%m')}/{filename}"

    try:
        # 1. Download image to /tmp
        response = await client.get(image_url, timeout=30.0)
        response.raise_for_status()
        with open(local_temp_path, 'wb') as f:
            f.write(response.content)
        print(f"        Downloaded image to {local_temp_path}")

        # 2. Upload image from /tmp to S3
        s3_client.upload_file(local_temp_path, S3_BUCKET_NAME, s3_key)
        s3_path = f"s3://{S3_BUCKET_NAME}/{s3_key}"
        print(f"        Successfully uploaded to {s3_path}")
        
        return s3_path

    except httpx.RequestError as e:
        print(f"        Error downloading image {image_url}: {e}")
        return None
    except ClientError as e:
        print(f"        Error uploading {filename} to S3: {e}")
        return None
    except Exception as e:
        print(f"        An unexpected error occurred in download/upload: {e}")
        return None
    finally:
        # 3. Clean up the /tmp file
        if os.path.exists(local_temp_path):
            os.remove(local_temp_path)

def parse_showtimes_from_html(soup: BeautifulSoup) -> dict:
    """Parses the cinema and showtime data from a given HTML soup object."""
    showtimes = {}
    showtimes_lists = soup.select('#ShowtimesList')
    if not showtimes_lists:
        return showtimes

    for showtimes_list in showtimes_lists:
        current_cinema = 'N/A'
        for element in (child for child in showtimes_list.children if getattr(child, 'name', None)):
            cinema_heading = element.find('b') if element.name in ('h3', 'a') else None
            if cinema_heading:
                current_cinema = cinema_heading.get_text(' ', strip=True)
                if current_cinema not in showtimes:
                    showtimes[current_cinema] = []
            elif element.name == 'div':
                times = [
                    time.get_text(' ', strip=True)
                    for time in element.select('.showbox')
                    if time.get_text(' ', strip=True)
                ]
                if times and current_cinema != 'N/A':
                    showtimes[current_cinema].extend(times)
    # Remove duplicates
    for cinema in showtimes:
        showtimes[cinema] = sorted(list(set(showtimes[cinema])))
    return showtimes

async def scrape_aggregated_showtimes(client: httpx.AsyncClient, showtimes_url: str) -> str:
    """
    Scrapes showtimes by reverse-engineering the ASP.NET form submissions
    for each date, avoiding the need for a full browser.
    """
    print(f"          Fetching showtimes from: {showtimes_url}")
    all_dates_data = {}
    try:
        response = await client.get(showtimes_url, headers=HTTP_HEADERS)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'lxml')

        date_dropdown = soup.select_one('select[id*="ddlShowdate"]')
        if not date_dropdown: return 'N/A'
        
        target_name = date_dropdown.get('name', 'ctl00$cphContent$ctl01$ddlShowdate')
        date_options = [(opt.get('value'), opt.text) for opt in date_dropdown.find_all('option') if opt.get('value')]
        date_options = date_options[:MAX_DAYS_TO_SCRAPE] if MAX_DAYS_TO_SCRAPE else date_options

        viewstate = soup.select_one("#__VIEWSTATE").get("value") if soup.select_one("#__VIEWSTATE") else ""
        eventvalidation = soup.select_one("#__EVENTVALIDATION").get("value") if soup.select_one("#__EVENTVALIDATION") else ""

        if not date_options: return 'N/A'
        print(f"            Processing Date: {date_options[0][1]}")
        first_date_showtimes = parse_showtimes_from_html(soup)
        all_dates_data[date_options[0][1]] = first_date_showtimes

        for i, (date_val, date_txt) in enumerate(date_options[1:]):
            print(f"            Processing Date: {date_txt}")
            await asyncio.sleep(REQUEST_DELAY)

            form_data = {
                '__EVENTTARGET': target_name, '__EVENTARGUMENT': '', '__LASTFOCUS': '',
                '__VIEWSTATE': viewstate, '__EVENTVALIDATION': eventvalidation,
                target_name: date_val,
            }
            post_response = await client.post(showtimes_url, data=form_data, headers=HTTP_HEADERS)
            
            if post_response.status_code != 200:
                print(f"            Request failed with status {post_response.status_code}. Stopping showtime scrape for this movie.")
                break

            post_soup = BeautifulSoup(post_response.text, 'lxml')
            all_dates_data[date_txt] = parse_showtimes_from_html(post_soup)

            if new_viewstate := post_soup.select_one("#__VIEWSTATE"): viewstate = new_viewstate.get("value")
            if new_eventvalidation := post_soup.select_one("#__EVENTVALIDATION"): eventvalidation = new_eventvalidation.get("value")

        cinema_map = {}
        for date_str, cinemas in all_dates_data.items():
            for cinema_name, times in cinemas.items():
                if cinema_name not in cinema_map:
                    cinema_map[cinema_name] = {"cinemaName": cinema_name, "showings": []}
                cinema_map[cinema_name]["showings"].append({"date": date_str, "times": times})
        
        return json.dumps(list(cinema_map.values()), separators=(',', ':'))

    except Exception as e:
        print(f"          An error occurred in scrape_aggregated_showtimes: {e}")
        return 'N/A'

async def scraper_main():
    """Main asynchronous scraping logic."""
    print(f"Starting web scraping script v10.0 at {get_malaysian_time()}...")
    
    scraped_records = []
    
    async with httpx.AsyncClient(follow_redirects=True) as client:
        print(f"Fetching movie list from: {MOVIES_NOWSHOWING_URL}")
        main_page_response = await client.get(MOVIES_NOWSHOWING_URL, headers=HTTP_HEADERS)
        main_page_soup = BeautifulSoup(main_page_response.text, 'lxml')
        movie_listings = main_page_soup.select('div.MovieWrap')
        print(f"Found {len(movie_listings)} movie listings.")

        for listing in movie_listings[:MAX_MOVIES_TO_SCRAPE]:
            await asyncio.sleep(REQUEST_DELAY)
            title_element = listing.select_one('.mov-lg a, .mov-sm a')
            if not title_element: continue
            
            title = title_element.text.strip()
            movie_url = f"{BASE_URL}{title_element.get('href')}"
            print(f"      Processing movie: {title}")

            detail_page_response = await client.get(movie_url, headers=HTTP_HEADERS)
            detail_soup = BeautifulSoup(detail_page_response.text, 'lxml')
            
            container = detail_soup.select_one('.con-lg')
            if not container: continue
            
            description = next((node.strip() for node in container.children if isinstance(node, str) and len(node.strip()) > 50), 'N/A')
            container_text = container.get_text(separator='\n')
            
            def extract_metadata(p, t, flags=0): return (m.group(1).strip() if (m := re.search(p, t, flags)) else 'N/A')
            
            raw_metadata = {k: extract_metadata(f"^{k}\\s*:\\s*(.+)", container_text, re.MULTILINE | re.I) for k in ['Language', 'Classification', 'Release Date', 'Genre', 'Running Time', 'Distributor', 'Cast', 'Director', 'Format']}
            
            rt_str = raw_metadata.get('Running Time', 'N/A')
            h = int(h.group(1)) * 60 if (h := re.search(r'(\d+)\s*Hours?', rt_str, re.I)) else 0
            m = int(m.group(1)) if (m := re.search(r'(\d+)\s*Minutes?', rt_str, re.I)) else 0
            total_minutes = h + m if h + m > 0 else 'N/A'
            
            formatted_date = 'N/A'
            try: formatted_date = datetime.strptime(raw_metadata.get('Release Date', 'N/A'), '%d %b %Y').strftime('%Y-%m-%d')
            except (ValueError, TypeError): pass

            poster_url = detail_soup.select_one('#ctl00_cphContent_imgPoster')['src'] if detail_soup.select_one('#ctl00_cphContent_imgPoster') else 'N/A'
            s3_poster_path = await download_and_upload_poster_to_s3(client, poster_url)

            showtimes_data = 'N/A'
            showtimes_link = next((link for link in detail_soup.select('#MovieSec .con-lg a') if "showtimes" in link.text.lower()), None)
            
            if showtimes_link:
                showtimes_url = f"{BASE_URL}{showtimes_link.get('href')}"
                showtimes_data = await scrape_aggregated_showtimes(client, showtimes_url)
            
            scraped_records.append({
                'Movie Title': title, 'Movie URL': movie_url, 'Description': description,
                'Running Time (Minutes)': total_minutes, 'Release Date (YYYY-MM-DD)': formatted_date,
                'Language': raw_metadata.get('Language', 'N/A'), 'Genre': raw_metadata.get('Genre', 'N/A'),
                'Distributor': raw_metadata.get('Distributor', 'N/A'), 'Classification': raw_metadata.get('Classification', 'N/A'),
                'Cast': raw_metadata.get('Cast', 'N/A'), 'Director': raw_metadata.get('Director', 'N/A'),
                'Format': raw_metadata.get('Format', 'N/A'),
                'Poster URL': poster_url, 'S3 Poster Path': s3_poster_path,
                'Aggregated Showtimes Data': showtimes_data,
                'Scrape Date': get_malaysian_time().strftime('%Y-%m-%d %H:%M:%S')
            })
    
    print(f"Scraping complete. Found {len(scraped_records)} movies.")
    return scraped_records

def lambda_handler(event, context):
    """
    AWS Lambda entry point. Triggers the scraper, then saves the result to S3.
    """
    if not S3_BUCKET_NAME:
        error_message = "CRITICAL ERROR: S3_BUCKET_NAME environment variable is not set."
        print(error_message)
        return {'statusCode': 500, 'body': json.dumps(error_message)}

    try:
        # Run the main async scraper function
        all_movie_data = asyncio.run(scraper_main())

        if all_movie_data:
            populated_showtime_records = 0
            for movie in all_movie_data:
                raw_showtimes = movie.get('Aggregated Showtimes Data')
                try:
                    parsed_showtimes = json.loads(raw_showtimes) if isinstance(raw_showtimes, str) else raw_showtimes
                except (TypeError, json.JSONDecodeError):
                    parsed_showtimes = []
                if isinstance(parsed_showtimes, list) and parsed_showtimes:
                    populated_showtime_records += 1

            if populated_showtime_records == 0:
                error_message = (
                    f"Scraped {len(all_movie_data)} movies but found no showtimes. "
                    "The existing S3 movie.json was preserved."
                )
                print(error_message)
                return {'statusCode': 500, 'body': json.dumps(error_message)}

            json_s3_key = 'movie.json'
            
            # Save the final JSON data to S3, overwriting the previous file
            s3_client.put_object(
                Bucket=S3_BUCKET_NAME,
                Key=json_s3_key,
                Body=json.dumps(all_movie_data, indent=2),
                ContentType='application/json'
            )
            
            success_message = f"Successfully scraped {len(all_movie_data)} movies and saved to s3://{S3_BUCKET_NAME}/{json_s3_key}"
            print(success_message)
            return {'statusCode': 200, 'body': json.dumps(success_message)}
        else:
            print("No movie data was scraped. S3 was not updated.")
            return {'statusCode': 200, 'body': json.dumps("No movie data was scraped.")}

    except Exception as e:
        # Log the full traceback for debugging in CloudWatch
        print(f"CRITICAL TOP-LEVEL ERROR: {e}")
        traceback.print_exc()
        return {'statusCode': 500, 'body': json.dumps(f"An error occurred: {e}")}