"""
Corre cada 5 horas via GitHub Actions.
1. Lee todos los usuarios anotados en Supabase (con sus idiomas, modalidad y pais).
2. Lee ofertas de portales de empleo publicos (RSS + JSON), UNA sola vez por
   corrida (no una vez por usuario), para no sobrecargar ningun servicio.
3. Filtra las que son junior/trainee y matchean con las palabras clave/skills
   de cada usuario.
4. Arma un resumen de perfil (idiomas -> regiones con alcance) y lo incluye
   arriba de cada mail.
5. Manda un mail (via Brevo) con las nuevas ofertas a cada usuario.
6. Registra lo ya enviado para no repetir avisos.
"""
import os
import re
import time
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
REQUEST_HEADERS = {"User-Agent": "alertas-empleo-bot/1.0 (+github actions, uso personal)"}

# Cada feed se pide UNA sola vez por corrida del robot (cada 5hs), sin
# importar cuantos usuarios haya, para no golpear ningun portal con
# pedidos repetidos. "lang" es el idioma predominante de esa fuente.
RSS_FEEDS = [
    {"url": "https://weworkremotely.com/categories/remote-programming-jobs.rss", "lang": "en"},
    {"url": "https://weworkremotely.com/categories/remote-design-jobs.rss", "lang": "en"},
    {"url": "https://weworkremotely.com/categories/remote-marketing-jobs.rss", "lang": "en"},
    {"url": "https://remotive.com/feed/jobs", "lang": "en"},
    {"url": "https://remoteok.com/remote-jobs.rss", "lang": "en"},
    {"url": "https://www.arbeitnow.com/feed", "lang": "en"},
    {"url": "https://www.workingnomads.com/jobs.rss", "lang": "en"},
]

JSON_FEEDS = [
    {"url": "https://himalayas.app/jobs/api", "lang": "en", "kind": "himalayas"},
    {"url": "https://jobicy.com/api/v2/remote-jobs?count=50", "lang": "en", "kind": "jobicy"},
]

JUNIOR_TERMS = [
    "junior", "trainee", "entry level", "entry-level", "sin experiencia",
    "becario", "beca", "primer empleo", "graduate", "asistente", "aprendiz",
]

LANG_REGIONS = {
    "es": "España y toda Latinoamerica",
    "en": "practicamente todo el mundo (USA, UK, Canada, Australia, Europa, India y mas)",
    "pt": "Brasil y Portugal",
    "fr": "Francia, Belgica, Canada (Quebec) y Africa francofona",
    "de": "Alemania, Austria y Suiza",
    "it": "Italia",
}

WORK_MODE_LABEL = {
    "remoto_mundial": "remoto, sin importar el pais",
    "remoto_pais": "remoto dentro de tu pais",
    "presencial": "presencial en tu pais",
    "cualquiera": "remoto o presencial, sin restriccion",
}


def clean(html):
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


def fetch_rss():
    jobs = []
    for feed_cfg in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_cfg["url"], request_headers=REQUEST_HEADERS)
            for e in feed.entries[:40]:
                jobs.append({
                    "title": e.get("title", ""),
                    "desc": clean(e.get("summary", "") or e.get("description", "")),
                    "link": e.get("link", ""),
                    "lang": feed_cfg["lang"],
                })
        except Exception as ex:
            print(f"Error leyendo {feed_cfg['url']}: {ex}")
    return jobs


def fetch_json():
    jobs = []
    for feed_cfg in JSON_FEEDS:
        try:
            r = requests.get(feed_cfg["url"], headers=REQUEST_HEADERS, timeout=20)
            r.raise_for_status()
            data = r.json()
            if feed_cfg["kind"] == "himalayas":
                for j in (data.get("jobs") or [])[:60]:
                    jobs.append({
                        "title": j.get("title", ""),
                        "desc": clean(j.get("description", "") or j.get("excerpt", "")),
                        "link": j.get("applicationLink") or f"https://himalayas.app/jobs/{j.get('guid','')}",
                        "lang": feed_cfg["lang"],
                    })
            elif feed_cfg["kind"] == "jobicy":
                for j in (data.get("jobs") or [])[:60]:
                    jobs.append({
                        "title": j.get("jobTitle", j.get("title", "")),
                        "desc": clean(j.get("jobDescription", "") or j.get("jobExcerpt", "")),
                        "link": j.get("url", ""),
                        "lang": feed_cfg["lang"],
                    })
        except Exception as ex:
            print(f"Error leyendo {feed_cfg['url']}: {ex}")
        time.sleep(1)  # pequeña pausa entre plataformas distintas, por prolijidad
    return jobs


def fetch_jobs():
    jobs = fetch_rss() + fetch_json()
    # de-duplicar por link
    seen, unique = set(), []
    for j in jobs:
        if j["link"] and j["link"] not in seen:
            seen.add(j["link"])
            unique.append(j)
    return unique


def get_active_users():
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/users",
        headers=HEADERS,
        params={
            "select": "id,email,keywords,skills,areas,languages,work_mode,country",
            "active": "eq.true",
        },
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


def perfil_resumen(user):
    langs = user.get("languages") or []
    mode = user.get("work_mode") or "remoto_mundial"
    mode_txt = WORK_MODE_LABEL.get(mode, mode)
    if langs:
        regiones = "; ".join(LANG_REGIONS.get(l, l) for l in langs)
    else:
        regiones = LANG_REGIONS["en"]
    pais = user.get("country")
    pais_txt = f" ({pais})" if pais else ""
    return (
        f"Buscamos ofertas <b>{mode_txt}</b>{pais_txt}. Por tus idiomas, tu alcance potencial es: {regiones}. "
        f"Por ahora las fuentes automaticas son principalmente en ingles; a medida que sumemos mas portales "
        f"locales vas a recibir tambien ofertas en tus otros idiomas."
    )


def send_email(to_email, jobs, user):
    rows = ""
    for j in jobs:
        rows += f"""
        <tr>
          <td style="padding:12px 0;border-bottom:1px solid #eee">
            <a href="{j['link']}" style="font-weight:bold;color:#2563eb;text-decoration:none">{j['title']}</a>
            <p style="margin:4px 0 0;color:#555;font-size:14px">{j['desc'][:220]}...</p>
          </td>
        </tr>"""
    resumen = perfil_resumen(user)
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
      <h2 style="background:linear-gradient(90deg,#6d28d9,#db2777);-webkit-background-clip:text;background-clip:text;color:transparent">Nuevas ofertas para vos</h2>
      <div style="background:#f4f0ff;border:1px solid #e4d9ff;border-radius:10px;padding:12px 16px;font-size:13px;color:#4c1d95;margin-bottom:16px">
        {resumen}
      </div>
      <p>Encontramos {len(jobs)} oferta(s) junior/trainee que podrian matchear con tu perfil.</p>
      <table style="width:100%;border-collapse:collapse">{rows}</table>
      <p style="color:#999;font-size:12px;margin-top:24px">
        Recibis esto porque te registraste en el buscador automatico de empleo.
      </p>
    </div>"""

    payload = {
        "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
        "to": [{"email": to_email}],
        "subject": f"🔎 {len(jobs)} nuevas ofertas junior/trainee para vos",
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
            send_email(user["email"], matches, user)
            for j in matches:
                mark_sent(user["id"], j["link"])
        else:
            print(f"Sin novedades para {user['email']}")

    print("Listo:", datetime.now(timezone.utc).isoformat())


if __name__ == "__main__":
    main()
