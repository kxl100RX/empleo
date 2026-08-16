"""
Corre cada 5 horas via GitHub Actions.
1. Lee todos los usuarios anotados en Supabase (con su rubro, nivel de
   experiencia, idiomas, modalidad y pais).
2. Lee ofertas de portales de empleo publicos (RSS + JSON), UNA sola vez por
   corrida (no una vez por usuario), para no sobrecargar ningun servicio.
3. Matchea con las palabras clave/skills/rubro de cada usuario y, si eligio
   un nivel de experiencia especifico (junior, semi senior, senior,
   gerencial), filtra tambien por eso. Si eligio "cualquiera", no filtra
   por nivel: sirve para cualquier persona, seniority y rubro.
4. Arma un resumen de perfil (nivel, idiomas -> regiones con alcance) y lo
   incluye arriba de cada mail.
5. Manda un mail (via Brevo) con las nuevas ofertas a cada usuario.
6. Registra lo ya enviado para no repetir avisos.
"""
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote

import feedparser
import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
BREVO_API_KEY = os.environ["BREVO_API_KEY"]
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "alertas@example.com")
SENDER_NAME = os.environ.get("SENDER_NAME", "Alertas de Empleo")
TRACKER_URL = "https://kxl100rx.github.io/empleo/recursos/tracker-busqueda-laboral.xlsx"

HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
}
REQUEST_HEADERS = {"User-Agent": "alertas-empleo-bot/1.0 (+github actions, uso personal)"}

# Cada feed se pide UNA sola vez por corrida del robot (cada 5hs), sin
# importar cuantos usuarios haya, para no golpear ningun portal con
# pedidos repetidos. "lang" es el idioma predominante de esa fuente.
# Son feeds generales (todo rubro y todo nivel), no solo tech ni solo junior.
RSS_FEEDS = [
    {"url": "https://weworkremotely.com/categories/remote-programming-jobs.rss", "lang": "en"},
    {"url": "https://weworkremotely.com/categories/remote-design-jobs.rss", "lang": "en"},
    {"url": "https://weworkremotely.com/categories/remote-marketing-jobs.rss", "lang": "en"},
    {"url": "https://weworkremotely.com/categories/remote-customer-support-jobs.rss", "lang": "en"},
    {"url": "https://weworkremotely.com/categories/remote-sales-and-marketing-jobs.rss", "lang": "en"},
    {"url": "https://weworkremotely.com/categories/remote-management-and-finance-jobs.rss", "lang": "en"},
    {"url": "https://remotive.com/feed/jobs", "lang": "en"},
    {"url": "https://remoteok.com/remote-jobs.rss", "lang": "en"},
    {"url": "https://www.arbeitnow.com/feed", "lang": "en"},
    {"url": "https://www.workingnomads.com/jobs.rss", "lang": "en"},
]

JSON_FEEDS = [
    {"url": "https://himalayas.app/jobs/api", "lang": "en", "kind": "himalayas"},
    {"url": "https://jobicy.com/api/v2/remote-jobs?count=50", "lang": "en", "kind": "jobicy"},
]

# Terminos que ayudan a reconocer el nivel de experiencia de un aviso.
# Solo se usan si el usuario eligio un nivel especifico (no "cualquiera").
SENIORITY_TERMS = {
    "junior": [
        "junior", "trainee", "entry level", "entry-level", "sin experiencia",
        "becario", "beca", "primer empleo", "graduate", "asistente", "aprendiz",
    ],
    "semi_senior": [
        "semi senior", "semi-senior", "ssr.", " ssr ", "mid level", "mid-level",
        "intermedio", "intermediate",
    ],
    "senior": [
        "senior", " sr.", " sr ", "lead", "principal", "staff",
    ],
    "gerencial": [
        "gerente", "manager", "director", "head of", "jefe", "chief", "vp ", "vicepresidente",
    ],
}

SENIORITY_LABEL = {
    "cualquiera": "cualquier nivel de experiencia",
    "junior": "Junior / Trainee",
    "semi_senior": "Semi Senior",
    "senior": "Senior",
    "gerencial": "Gerencial / Directivo",
}

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


# ---------------------------------------------------------------------
# Kit de Búsqueda Laboral: cadenas de búsqueda + deep-links a portales
# que no podemos integrar (LinkedIn, Indeed, Computrabajo, etc). Misma
# lógica que el generador del formulario (index.html), portada a Python
# para poder mandarla por mail una sola vez, apenas alguien se anota.
# ---------------------------------------------------------------------
ROLE_MAP_BASE = {
    "Programación / IT": ["Desarrollador/a", "Analista de Sistemas"],
    "Ventas": ["Ejecutivo/a de Ventas", "Account Executive"],
    "Marketing": ["Analista de Marketing Digital"],
    "Datos": ["Data Analyst"],
    "Finanzas": ["Analista Financiero"],
    "Recursos Humanos": ["Analista de RRHH"],
    "Administración": ["Analista Administrativo/a"],
    "Salud": ["Profesional de la salud"],
    "Ingeniería": ["Ingeniero/a de Proyecto"],
    "Diseño": ["Diseñador/a UI/UX"],
    "Logística": ["Analista de Logística / Supply Chain"],
    "Legal": ["Asistente Legal / Paralegal"],
}

COUNTRY_CODE_MAP = {
    "argentina": "ar", "méxico": "mx", "mexico": "mx", "chile": "cl", "colombia": "co",
    "perú": "pe", "peru": "pe", "ecuador": "ec", "uruguay": "uy", "paraguay": "py",
    "bolivia": "bo", "venezuela": "ve", "panamá": "pa", "panama": "pa", "guatemala": "gt",
    "honduras": "hn", "el salvador": "sv", "nicaragua": "ni", "costa rica": "cr",
    "república dominicana": "do", "republica dominicana": "do", "puerto rico": "pr",
    "españa": "es", "spain": "es", "estados unidos": "us", "usa": "us", "united states": "us",
    "brasil": "br", "brazil": "br",
}
INDEED_DOMAINS = {"ar": "ar.indeed.com", "mx": "mx.indeed.com", "cl": "cl.indeed.com", "co": "co.indeed.com", "pe": "pe.indeed.com", "ec": "ec.indeed.com", "es": "es.indeed.com", "us": "www.indeed.com", "uy": "uy.indeed.com"}
COMPUTRABAJO_DOMAINS = {"ar": "ar.computrabajo.com", "mx": "mx.computrabajo.com", "co": "co.computrabajo.com", "cl": "cl.computrabajo.com", "pe": "pe.computrabajo.com", "ec": "ec.computrabajo.com"}
BUMERAN_DOMAINS = {"ar": "www.bumeran.com.ar", "mx": "www.bumeran.com.mx", "pe": "www.bumeran.com.pe"}


def slugify(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def detectar_codigo_pais(pais):
    if not pais:
        return None
    t = pais.strip().lower()
    for name, code in COUNTRY_CODE_MAP.items():
        if name in t:
            return code
    return None


def generar_palabras_simples(areas, skills):
    roles = []
    for a in areas:
        roles += ROLE_MAP_BASE.get(a, [])
    roles = list(dict.fromkeys(roles))
    base = roles[:2] if roles else areas[:2]
    top = (skills or [])[:3]
    combined = list(dict.fromkeys([*base, *top]))
    return ", ".join(combined) if combined else "empleo"


def generar_boolean_search(areas, seniority, skills):
    roles = []
    for a in areas:
        roles += ROLE_MAP_BASE.get(a, [])
    roles = list(dict.fromkeys(roles))
    role_group = "(" + " OR ".join(f'"{r}"' for r in roles[:4]) + ")" if roles else ""
    skill_group = "(" + " OR ".join(f'"{s}"' for s in (skills or [])[:4]) + ")" if skills else ""
    nivel_map = {
        "junior": '("junior" OR "trainee" OR "sin experiencia")',
        "semi_senior": '"semi senior"',
        "senior": '"senior"',
        "gerencial": '("gerente" OR "director")',
    }
    nivel_group = nivel_map.get(seniority, "")
    parts = [p for p in (role_group, skill_group, nivel_group) if p]
    return " AND ".join(parts) if parts else "empleo"


def generar_kit_links(areas, seniority, skills, country):
    cc = detectar_codigo_pais(country) or "ar"
    simple = generar_palabras_simples(areas, skills)
    boolean = generar_boolean_search(areas, seniority, skills)
    primer_termino = simple.split(",")[0].strip()
    q_simple = quote(simple)
    q_boolean = quote(boolean)
    q_loc = quote(country) if country else ""
    slug = slugify(primer_termino) or "empleo"

    indeed_host = INDEED_DOMAINS.get(cc, "www.indeed.com")
    ct_host = COMPUTRABAJO_DOMAINS.get(cc, "www.computrabajo.com")
    bm_host = BUMERAN_DOMAINS.get(cc, "www.bumeran.com.ar")

    links = [
        ("LinkedIn", f"https://www.linkedin.com/jobs/search/?keywords={q_boolean}" + (f"&location={q_loc}" if country else "")),
        ("Indeed", f"https://{indeed_host}/jobs?q={q_boolean}" + (f"&l={q_loc}" if country else "")),
        ("Computrabajo", f"https://{ct_host}/trabajo-de-{slug}"),
        ("Bumeran", f"https://{bm_host}/empleos-busqueda-{slug}.html"),
        ("Glassdoor", f"https://www.glassdoor.com/Job/jobs.htm?sc.keyword={q_simple}"),
        ("InfoJobs (España)", f"https://www.infojobs.net/jobsearch/search-results/list.xhtml?keyword={q_simple}"),
    ]
    return simple, boolean, links


def send_kit_email(user):
    """Manda, UNA sola vez por usuario, el Kit de Búsqueda Laboral: las
    cadenas de búsqueda + los links a portales grandes + la planilla de
    seguimiento. Devuelve False si no se pudo mandar (para no marcarlo
    como enviado y reintentar en la próxima corrida)."""
    areas = user.get("areas") or []
    if not areas:
        print(f"Sin rubro para armar el kit de {user['email']}, se omite (se reintenta cuando cargue un rubro)")
        return False

    seniority = user.get("seniority") or "cualquiera"
    skills = list(dict.fromkeys((user.get("skills") or []) + (user.get("keywords") or [])))
    country = user.get("country")

    simple, boolean, links = generar_kit_links(areas, seniority, skills, country)

    links_html = "".join(f"""
      <tr>
        <td style="padding:8px 0;border-bottom:1px solid #eee;font-size:14px">
          <b>{name}</b> — <a href="{url}" style="color:#2563eb;text-decoration:none">Abrir búsqueda y crear alerta →</a>
        </td>
      </tr>""" for name, url in links)

    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
      <h2 style="background:linear-gradient(90deg,#6d28d9,#db2777);-webkit-background-clip:text;background-clip:text;color:transparent">🎁 Tu Kit de Búsqueda Laboral</h2>
      <p style="color:#444;font-size:14px">Armamos esto con tu perfil para que también puedas activar alertas en los
      portales que nosotros no podemos revisar automáticamente (LinkedIn, Indeed, Computrabajo y más).
      <b>Guardá este mail</b> — esto no queda guardado en ningún otro lado.</p>

      <p style="font-size:13px;color:#52525b;font-weight:bold;margin-bottom:4px">Búsqueda simple — pegala en Computrabajo, Bumeran o cualquier buscador básico:</p>
      <div style="background:#f8f8fb;border:1px solid #e4e4e7;border-radius:8px;padding:10px 12px;font-size:13px;margin-bottom:14px">{simple}</div>

      <p style="font-size:13px;color:#52525b;font-weight:bold;margin-bottom:4px">Búsqueda avanzada — pegala en LinkedIn, Indeed o buscadores con operadores:</p>
      <div style="background:#f8f8fb;border:1px solid #e4e4e7;border-radius:8px;padding:10px 12px;font-size:13px;margin-bottom:14px">{boolean}</div>

      <p style="font-size:13px;color:#52525b;font-weight:bold;margin-bottom:6px">O abrí la búsqueda ya armada con un clic:</p>
      <table style="width:100%;border-collapse:collapse;margin-bottom:16px">{links_html}</table>

      <div style="background:#ecfdf5;border:1px solid #a7f3d0;border-radius:10px;padding:14px 16px;margin-bottom:8px">
        <b>📊 Planilla de seguimiento:</b> organizá cada postulación, tus alertas y tu plan de capacitación.<br>
        <a href="{TRACKER_URL}" style="color:#16a34a;font-weight:bold;text-decoration:none">Descargar planilla →</a>
      </div>

      <p style="color:#999;font-size:12px;margin-top:20px">
        Los links son de mejor esfuerzo — si el país no matchea exacto, ajustalo dentro del portal.
        Nunca accedemos a tus cuentas: vos activás cada alerta con tu propio login.
      </p>
    </div>"""

    payload = {
        "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
        "to": [{"email": user["email"]}],
        "subject": "🎁 Tu Kit de Búsqueda Laboral está listo",
        "htmlContent": html,
    }
    r = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    print(f"Kit email a {user['email']}: status {r.status_code}")
    if r.status_code >= 300:
        print(r.text)
        return False
    return True


def get_users_pending_kit():
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/users",
        headers=HEADERS,
        params={
            "select": "id,email,areas,seniority,skills,keywords,country",
            "kit_email_sent": "eq.false",
            "active": "eq.true",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def mark_kit_sent(user_id):
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/users",
        headers=HEADERS,
        params={"id": f"eq.{user_id}"},
        json={"kit_email_sent": True},
        timeout=30,
    )


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
            "select": "id,email,keywords,skills,areas,languages,work_mode,country,seniority",
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


def matches_seniority(text, seniority):
    """Si el usuario eligio un nivel especifico, el aviso tiene que
    mencionarlo. Si eligio "cualquiera" (o no eligio nada), no filtra por
    nivel: sirve para cualquier persona y seniority."""
    if not seniority or seniority == "cualquiera":
        return True
    terms = SENIORITY_TERMS.get(seniority)
    if not terms:
        return True
    t = text.lower()
    return any(term in t for term in terms)


def match_score(job_text, user_terms):
    t = job_text.lower()
    return sum(1 for term in user_terms if term and term.lower() in t)


def perfil_resumen(user):
    langs = user.get("languages") or []
    mode = user.get("work_mode") or "remoto_mundial"
    mode_txt = WORK_MODE_LABEL.get(mode, mode)
    seniority = user.get("seniority") or "cualquiera"
    seniority_txt = SENIORITY_LABEL.get(seniority, seniority)
    if langs:
        regiones = "; ".join(LANG_REGIONS.get(l, l) for l in langs)
    else:
        regiones = LANG_REGIONS["en"]
    pais = user.get("country")
    pais_txt = f" ({pais})" if pais else ""
    return (
        f"Buscamos ofertas <b>{mode_txt}</b>{pais_txt} para nivel <b>{seniority_txt}</b>. "
        f"Por tus idiomas, tu alcance potencial es: {regiones}. "
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
      <p>Encontramos {len(jobs)} oferta(s) que podrian matchear con tu perfil.</p>
      <table style="width:100%;border-collapse:collapse">{rows}</table>
      <p style="color:#999;font-size:12px;margin-top:24px">
        Recibis esto porque te registraste en el buscador automatico de empleo.
      </p>
    </div>"""

    payload = {
        "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
        "to": [{"email": to_email}],
        "subject": f"🔎 {len(jobs)} nuevas ofertas de trabajo para vos",
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
    pending_kit = get_users_pending_kit()
    print(f"{len(pending_kit)} usuario(s) nuevo(s) esperando el Kit de Búsqueda Laboral")
    for user in pending_kit:
        if send_kit_email(user):
            mark_kit_sent(user["id"])

    users = get_active_users()
    all_jobs = fetch_jobs()
    print(f"{len(users)} usuarios activos, {len(all_jobs)} avisos leidos de los feeds")

    for user in users:
        user_terms = (
            (user.get("keywords") or [])
            + (user.get("skills") or [])
            + (user.get("areas") or [])
        )
        seniority = user.get("seniority") or "cualquiera"
        sent_links = get_sent_links(user["id"])

        matches = []
        for job in all_jobs:
            if job["link"] in sent_links:
                continue
            full_text = job["title"] + " " + job["desc"]
            if not matches_seniority(full_text, seniority):
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
