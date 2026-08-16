"""
Corre cada 5 horas via GitHub Actions.
1. Lee todos los usuarios anotados en Supabase.
2. Lee ofertas de portales de empleo (RSS publicos).
3. Filtra las que son junior/trainee y matchean con las palabras clave/skills
   de cada usuario.
4. Manda un mail (via Brevo) con las nuevas ofertas a cada usuario.
5. Registra lo ya enviado para no repetir avisos.
"""
import os
import re
from datetime import datetime, timezone

import feedparser
import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
BREVO_API_KEY = os.environ["BREVO_API_KEY"]
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "alertas@example.com")
SENDER_NAME = os.environ.get("SENDER_NAME", "Alertas de Empleo")

HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
}

FEEDS = [
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-design-jobs.rss",
    "https://weworkremotely.com/categories/remote-marketing-jobs.rss",
    "https://remotive.com/feed/jobs",
    "https://remoteok.com/remote-jobs.rss",
    "https://www.arbeitnow.com/feed",
]

JUNIOR_TERMS = [
    "junior", "trainee", "entry level", "entry-level", "sin experiencia",
    "becario", "beca", "primer empleo", "graduate", "asistente", "aprendiz",
]


def clean(html):
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


def fetch_jobs():
    jobs = []
    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:40]:
                jobs.append({
                    "title": e.get("title", ""),
                    "desc": clean(e.get("summary", "") or e.get("description", "")),
                    "link": e.get("link", ""),
                })
        except Exception as ex:
            print(f"Error leyendo {url}: {ex}")
    return jobs


def get_active_users():
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/users",
        headers=HEADERS,
        params={"select": "id,email,keywords,skills,areas", "active": "eq.true"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def get_sent_links(user_id):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/sent_jobs",
        headers=HEADERS,
        params={"select": "job_link", "user_id": f"eq.{user_id}"},
        timeout=30,
    )
    r.raise_for_status()
    return {row["job_link"] for row in r.json()}


def mark_sent(user_id, link):
    requests.post(
        f"{SUPABASE_URL}/rest/v1/sent_jobs",
        headers={**HEADERS, "Prefer": "resolution=ignore-duplicates"},
        json={"user_id": user_id, "job_link": link},
        timeout=30,
    )


def is_junior(text):
    t = text.lower()
    return any(term in t for term in JUNIOR_TERMS)


def match_score(job_text, user_terms):
    t = job_text.lower()
    return sum(1 for term in user_terms if term and term.lower() in t)


def send_email(to_email, jobs):
    rows = ""
    for j in jobs:
        rows += f"""
        <tr>
          <td style="padding:12px 0;border-bottom:1px solid #eee">
            <a href="{j['link']}" style="font-weight:bold;color:#2563eb;text-decoration:none">{j['title']}</a>
            <p style="margin:4px 0 0;color:#555;font-size:14px">{j['desc'][:220]}...</p>
          </td>
        </tr>"""
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
      <h2>Nuevas ofertas para vos</h2>
      <p>Encontramos {len(jobs)} oferta(s) junior/trainee que podrian matchear con tu perfil.</p>
      <table style="width:100%;border-collapse:collapse">{rows}</table>
      <p style="color:#999;font-size:12px;margin-top:24px">
        Recibis esto porque te registraste en el buscador automatico de empleo.
      </p>
    </div>"""

    payload = {
        "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
        "to": [{"email": to_email}],
        "subject": f"Nuevas {len(jobs)} ofertas junior/trainee para vos",
        "htmlContent": html,
    }
    r = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    print(f"Email a {to_email}: status {r.status_code}")
    if r.status_code >= 300:
        print(r.text)


def main():
    users = get_active_users()
    all_jobs = fetch_jobs()
    print(f"{len(users)} usuarios activos, {len(all_jobs)} avisos leidos de los feeds")

    for user in users:
        user_terms = (
            (user.get("keywords") or [])
            + (user.get("skills") or [])
            + (user.get("areas") or [])
        )
        sent_links = get_sent_links(user["id"])

        matches = []
        for job in all_jobs:
            if job["link"] in sent_links:
                continue
            full_text = job["title"] + " " + job["desc"]
            if not is_junior(full_text):
                continue
            if user_terms and match_score(full_text, user_terms) == 0:
                continue
            matches.append(job)

        matches = matches[:15]
        if matches:
            send_email(user["email"], matches)
            for j in matches:
                mark_sent(user["id"], j["link"])
        else:
            print(f"Sin novedades para {user['email']}")

    print("Listo:", datetime.now(timezone.utc).isoformat())


if __name__ == "__main__":
    main()
