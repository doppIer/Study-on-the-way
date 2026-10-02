# Study on the Way

Turn a lecture-notes PDF into an audio podcast you can listen to on the way. Built for the AWS Zero to Shipped hackathon.

Live app: https://c77oy6jt7oymmqxkt5hveplasm0dqkgy.lambda-url.eu-central-1.on.aws/

## What it does

Upload a text-based PDF (up to 4 MB), choose a narration language (Turkish or English), a format (single narrator or two hosts) and a length of 2 to 8 minutes. The app returns an MP3 lesson and a transcript.

## Architecture

- One Lambda Function URL serves the web page and the API (`src/app.py`, `src/index.html`)
- The browser uploads the PDF to a private S3 bucket with a presigned URL
- A worker Lambda (`src/worker.py`) extracts the text with pypdf and asks Amazon Bedrock (Converse API) to write a spoken script
- Amazon Polly records the MP3, with two voices in conversation mode
- DynamoDB tracks job status, S3 lifecycle rules delete files after 1 day, and the PDF is deleted once the audio is ready
- A daily quota of 100 podcasts and a 4 MB PDF limit keep costs under control

If Bedrock is unavailable (for example when account quotas are 0), the worker reads the first part of the notes aloud with Polly and the result page says so.

## Deploy

Requirements: AWS account, AWS SAM CLI, Python 3.13.

```
sam build
sam deploy --stack-name ders-podcast --region eu-central-1 --capabilities CAPABILITY_IAM --resolve-s3
```

The stack output `SiteUrl` is the public address.

Parameters:

- `ModelId`: Bedrock model or inference profile id (default `eu.amazon.nova-lite-v1:0`)
- `BedrockRegion`: region used for Bedrock calls (default `eu-north-1`)
- `DailyLimit`: podcasts allowed per day (default `100`)

## Project layout

```
template.yaml      SAM template (Lambda x2, S3, DynamoDB)
src/app.py         API and page handler
src/worker.py      PDF text, Bedrock script, Polly audio
src/index.html     Single-file web UI
```
