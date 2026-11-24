# myCinemaz scraper

AWS Lambda scraper for the Cinema Online movie and showtime feed consumed by
`frontend-myCinemaz`.

## Production resources

- Function: `myCinemaz-scraper`
- Region: `ap-southeast-5`
- Stack: `mycinemaz-scraper-stack`
- Bucket: `movie-scraper-data-339712939731`
- Object: `movie.json`
- Entry point: `lambda_function.lambda_handler`
- Runtime: Python 3.11
- Schedule: `DailyMovieScrapeTrigger`

The frontend reads the feed from:

```text
https://movie-scraper-data-339712939731.s3.ap-southeast-5.amazonaws.com/movie.json
```

## Data source contract

`Aggregated Showtimes Data` must be a JSON string containing a list:

```json
[
  {
    "cinemaName": "GSC - Mid Valley, Mid Valley City",
    "showings": [
      {
        "date": "24 November 2026, Tuesday",
        "times": ["02:30PM", "06:55PM"]
      }
    ]
  }
]
```

The frontend parses string or array values, but the production scraper writes
the JSON string form.

## 24 November 2026 parsing incident

Cinema Online changed the relevant markup from a direct cinema link to:

```html
<h3><a><b>Cinema name</b></a></h3>
```

It also changed the page from one `#ShowtimesList` containing all cinemas to
one duplicate-id `#ShowtimesList` container per cinema. The Lambda previously
parsed zero cinemas from valid pages, produced `[]` for every movie, and
overwrote S3 without failing.

Commit `dd9ca76` fixes this by:

- selecting every `#ShowtimesList`;
- accepting old direct `<a><b>` headings and new `<h3><a><b>` headings;
- finding the ASP.NET date dropdown by `select[id*="ddlShowdate"]`;
- using the dropdown's actual `name` for postbacks;
- refusing the S3 upload when every scraped movie has an empty/invalid
  showtime value.

The `POST` responses use the same parsing path; test more than the initial
date when changing this code.

## Verification

After any parser change, run a focused live-source check. For the known
Hunger Games fixture, a healthy parse in November 2026 produced 134 cinemas,
294 date/cinema showings and 1,567 time values across five days. Counts vary
with the source, but zero is not valid when the source page visibly contains
cinemas.

Also compile the handler:

```powershell
python -m py_compile lambda_function.py
```

After deployment:

1. Invoke the Lambda.
2. Check CloudWatch for all expected `Processing Date` lines and the final
   success summary.
3. Download `movie.json`.
4. Parse every `Aggregated Showtimes Data` field and confirm more than zero
   movies have non-empty arrays.
5. Only then rebuild/deploy the frontend.

Never rely on `Successfully scraped N movies` alone. It previously appeared
after a complete all-empty scrape.

## Deployment

Preferred SAM flow (requires Docker or Python 3.11 available to SAM):

```powershell
sam build
sam deploy --no-confirm-changeset
```

The repository currently has no GitHub Actions workflow. If SAM cannot build,
preserve the existing Lambda dependency bundle and replace only
`lambda_function.py` in the deployed ZIP, then validate the archive before
using `aws lambda update-function-code`.

Useful AWS checks:

```powershell
aws lambda get-function --function-name myCinemaz-scraper
aws logs describe-log-streams --log-group-name /aws/lambda/myCinemaz-scraper --order-by LastEventTime --descending --limit 3
aws lambda invoke --function-name myCinemaz-scraper --invocation-type Event --cli-binary-format raw-in-base64-out --payload '{}' response.json
```
