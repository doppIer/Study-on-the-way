import io
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config
from pypdf import PdfReader

BUCKET = os.environ["BUCKET"]
TABLE = os.environ["TABLE"]
MODEL_ID = os.environ["MODEL_ID"]

s3 = boto3.client("s3")
bedrock = boto3.client(
    "bedrock-runtime",
    region_name=os.environ["BEDROCK_REGION"],
    config=Config(retries={"max_attempts": 2}),
)
polly = boto3.client("polly")
table = boto3.resource("dynamodb").Table(TABLE)

MAX_PDF = 4 * 1024 * 1024
CHUNK = 2500
MAX_CHARS = 8000

VOICES = {
    "en": {"A": ("Joanna", "neural"), "B": ("Matthew", "neural")},
    "tr": {"A": ("Burcu", "neural"), "B": ("Filiz", "standard")},
}
FALLBACK = {"en": ("Joanna", "standard"), "tr": ("Filiz", "standard")}
LANG_NAME = {"en": "English", "tr": "Turkish"}


class UserError(Exception):
    pass


def build_prompt(lang, mode, minutes):
    words = minutes * 140
    if mode == "dialog":
        form = (
            "Write a podcast dialogue between two hosts. Every line must start with "
            "'A:' or 'B:'. Host A explains the topic. Host B asks short curious "
            "questions and gives everyday examples. Keep each line under 60 words."
        )
    else:
        form = "Write a podcast monologue for a single host."
    return (
        "You turn lecture notes into an audio lesson. "
        + form
        + " Language: "
        + LANG_NAME[lang]
        + ". Target length: about "
        + str(words)
        + " words. Cover the key ideas of the provided notes in a logical order, "
        "explain terms simply, and finish with a three point recap. Use plain spoken "
        "sentences only: no markdown, no bullet points, no stage directions, no "
        "emojis, and say formulas in words instead of symbols. The first line must "
        "be 'TITLE: ' followed by a short title. Then a blank line, then the script. "
        "Use only information from the notes."
    )


def pdf_text(data):
    reader = PdfReader(io.BytesIO(data))
    parts = []
    total = 0
    for page in reader.pages:
        t = page.extract_text() or ""
        parts.append(t)
        total += len(t)
        if total >= MAX_CHARS:
            break
    return "\n".join(parts)[:MAX_CHARS].strip()


def clean(text):
    text = re.sub(r"[*#`_>]", "", text)
    return text.replace("---", "").strip()


def parse(text):
    lines = text.splitlines()
    title = "Lecture podcast"
    if lines and lines[0].strip().upper().startswith("TITLE:"):
        title = lines[0].strip()[6:].strip() or title
        lines = lines[1:]
    return title, "\n".join(lines).strip()


def split_text(text, limit=CHUNK):
    sentences = re.split(r"(?<=[.!?])\s+", text.replace("\n", " ").strip())
    parts = []
    current = ""
    for s in sentences:
        if current and len(current) + len(s) + 1 > limit:
            parts.append(current)
            current = s
        else:
            current = (current + " " + s).strip()
    if current:
        parts.append(current)
    return [p[:3000] for p in parts if p]


def segments(script, mode):
    if mode == "dialog":
        out = []
        for line in script.splitlines():
            m = re.match(r"^\s*([AB])\s*:\s*(.+)$", line)
            if m:
                for p in split_text(m.group(2)):
                    out.append((m.group(1), p))
        if out:
            return out
    return [("A", p) for p in split_text(script)]


def read_aloud_segments(script, mode):
    parts = split_text(script, 500)
    return [("AB"[i % 2] if mode == "dialog" else "A", p) for i, p in enumerate(parts)]


def synth(text, voice, engine):
    r = polly.synthesize_speech(
        Text=text, OutputFormat="mp3", VoiceId=voice, Engine=engine
    )
    return r["AudioStream"].read()


def say(args):
    lang, who, text = args
    voice, engine = VOICES[lang][who]
    try:
        return synth(text, voice, engine)
    except Exception as e:
        print(repr(e))
        voice, engine = FALLBACK[lang]
        return synth(text, voice, engine)


def run(job_id, lang, mode, minutes):
    key = "uploads/" + job_id + ".pdf"
    head = s3.head_object(Bucket=BUCKET, Key=key)
    if head["ContentLength"] > MAX_PDF:
        raise UserError("The PDF cannot be larger than 4 MB.")
    pdf = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    notes = pdf_text(pdf)
    if len(notes) < 50:
        raise UserError("No text could be read from the PDF. Upload a text-based PDF, not a scan.")
    note = ""
    try:
        r = bedrock.converse(
            modelId=MODEL_ID,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"text": "LECTURE NOTES:\n" + notes + "\n\n" + build_prompt(lang, mode, minutes)},
                    ],
                }
            ],
            inferenceConfig={"maxTokens": minutes * 450 + 300, "temperature": 0.4},
        )
        text = r["output"]["message"]["content"][0]["text"]
        title, script = parse(clean(text))
        if not script:
            raise ValueError("bos senaryo")
        parts = segments(script, mode)
    except Exception as e:
        print(repr(e))
        note = "The AI summary is unavailable right now, so the first part of your notes was read aloud."
        title = "Lecture notes (read aloud)"
        cut = notes[: minutes * 900]
        end = max(cut.rfind(". "), cut.rfind(".\n"))
        script = clean(cut[: end + 1] if end > 200 else cut)
        parts = read_aloud_segments(script, mode)
    jobs = [(lang, who, t) for who, t in parts]
    with ThreadPoolExecutor(4) as pool:
        audio = b"".join(pool.map(say, jobs))
    audio_key = "audio/" + job_id + ".mp3"
    s3.put_object(Bucket=BUCKET, Key=audio_key, Body=audio, ContentType="audio/mpeg")
    s3.delete_object(Bucket=BUCKET, Key=key)
    table.put_item(
        Item={
            "id": job_id,
            "status": "done",
            "title": title,
            "script": script,
            "note": note,
            "audioKey": audio_key,
            "exp": int(time.time()) + 86400,
        }
    )


def handler(event, context):
    job_id = event["id"]
    try:
        run(job_id, event["lang"], event["mode"], event["minutes"])
    except Exception as e:
        print(repr(e))
        msg = str(e) if isinstance(e, UserError) else "Processing failed, please try again."
        table.put_item(
            Item={
                "id": job_id,
                "status": "error",
                "msg": msg,
                "exp": int(time.time()) + 86400,
            }
        )
