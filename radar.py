from __future__ import annotations

import email
import html
import imaplib
import logging
import os
import random
import re
import smtplib
import sqlite3
import ssl
import time

from dataclasses import dataclass
from datetime import datetime
from email.header import decode_header, make_header
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv


# ==========================================================
# PFADE UND GRUNDEINSTELLUNGEN
# ==========================================================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATABASE_PATH = DATA_DIR / "radar.sqlite"
CONFIG_PATH = BASE_DIR / "config.yaml"
ENV_PATH = BASE_DIR / ".env"

DATA_DIR.mkdir(exist_ok=True)
load_dotenv(ENV_PATH)

LOGGER = logging.getLogger("schnaeppchen_radar")
TIMEZONE = ZoneInfo("Europe/Berlin")


# ==========================================================
# REGULAERE AUSDRUECKE
# ==========================================================

ANZEIGE_LINK_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?kleinanzeigen\.de/s-anzeige/[^\s\"'<>]+",
    re.IGNORECASE,
)

ANZEIGE_ID_PATTERN = re.compile(r"/(\d{6,})(?:-|/|$)")

PREIS_PATTERN = re.compile(
    r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?)\s*€",
    re.IGNORECASE,
)

PLZ_PATTERN = re.compile(
    r"\b(\d{5})\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9.\- ]{1,40})"
)


# ==========================================================
# DATENKLASSE
# ==========================================================

@dataclass
class Anzeige:
    id: str
    url: str
    titel: str
    preis_text: str = ""
    ort: str = ""
    preis_eur: float | None = None
    verschenken: bool = False
    vb: bool = False
    kategorie: str = ""
    kategorie_name: str = ""
    emoji: str = "📦"
    prioritaet: int = 0
    nachrichtenvorschlag: str = ""
    gefunden_am: int = 0


# ==========================================================
# KONFIGURATION UND UMGEBUNGSVARIABLEN
# ==========================================================

def lade_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Die Konfigurationsdatei fehlt: {path}")

    with path.open("r", encoding="utf-8") as datei:
        return yaml.safe_load(datei) or {}


def env_wert(name: str, erforderlich: bool = True, standard: str = "") -> str:
    wert = os.getenv(name, standard).strip()

    if erforderlich and not wert:
        raise RuntimeError(f"{name} fehlt in der .env-Datei.")

    return wert


# ==========================================================
# HILFSFUNKTIONEN
# ==========================================================

def text_bereinigen(text: str | None) -> str:
    if not text:
        return ""

    return " ".join(text.replace("\xa0", " ").split()).strip()


def wortmenge(text: str) -> set:
    return set(re.findall(r"[a-zäöüß0-9]+", text.lower()))


def aktuelle_zeit() -> int:
    return int(time.time())


def formatiere_zeit(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, TIMEZONE).strftime("%d.%m.%Y %H:%M")


def sichere_url(url: str) -> str:
    url = html.unescape(url.strip())

    if url.startswith("//"):
        url = "https:" + url

    if not url.startswith("http"):
        url = "https://" + url.lstrip("/")

    teile = urlsplit(url)

    return urlunsplit(
        (
            teile.scheme or "https",
            teile.netloc or "www.kleinanzeigen.de",
            teile.path,
            "",
            "",
        )
    )


def extrahiere_anzeigen_id(url: str) -> str:
    match = ANZEIGE_ID_PATTERN.search(url)

    if match:
        return match.group(1)

    return url


# ==========================================================
# E-MAIL-HEADER UND FILTER
# ==========================================================

def dekodiere_header(message, name: str) -> str:
    raw = message.get(name, "")

    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def email_passt_zum_filter(message, config: dict) -> bool:
    filter_config = config.get("email_filter", {})

    absender = dekodiere_header(message, "From").lower()
    betreff = dekodiere_header(message, "Subject").lower()

    absender_begriffe = [
        str(wert).lower()
        for wert in filter_config.get("absender_enthaelt", ["kleinanzeigen"])
    ]

    betreff_begriffe = [
        str(wert).lower()
        for wert in filter_config.get("betreff_enthaelt", [])
    ]

    absender_passt = (not absender_begriffe) or any(
        begriff in absender for begriff in absender_begriffe
    )

    betreff_passt = (not betreff_begriffe) or any(
        begriff in betreff for begriff in betreff_begriffe
    )

    return absender_passt and betreff_passt


def extrahiere_html_teile(message) -> list:
    html_teile = []
    text_teile = []

    if message.is_multipart():
        teile = list(message.walk())
    else:
        teile = [message]

    for teil in teile:
        content_type = teil.get_content_type()

        if content_type not in ("text/html", "text/plain"):
            continue

        disposition = str(teil.get("Content-Disposition", "")).lower()

        if "attachment" in disposition:
            continue

        payload = teil.get_payload(decode=True)

        if payload is None:
            continue

        charset = teil.get_content_charset() or "utf-8"

        try:
            inhalt = payload.decode(charset, errors="replace")
        except LookupError:
            inhalt = payload.decode("utf-8", errors="replace")

        if content_type == "text/html":
            html_teile.append(inhalt)
        else:
            text_teile.append(inhalt)

    if html_teile:
        return html_teile

    # Reine Textmails: URLs in Links umwandeln, damit der Parser sie findet
    ergebnis = []

    for text in text_teile:
        sicher = html.escape(text)
        sicher = re.sub(
            r"(https?://[^\s<>\"']+)",
            r'<a href="\1">\1</a>',
            sicher,
        )
        ergebnis.append("<html><body><pre>" + sicher + "</pre></body></html>")

    return ergebnis


# ==========================================================
# GMAIL PER IMAP LESEN
# ==========================================================

def mail_holen(
    config: dict,
    nur_ungelesen: bool = True,
    maximale_anzahl: int = 100,
) -> list:
    host = env_wert("IMAP_HOST")
    port = int(env_wert("IMAP_PORT", erforderlich=False, standard="993"))
    benutzer = env_wert("IMAP_USER")
    passwort = env_wert("IMAP_PASSWORD")
    ordner = env_wert("IMAP_FOLDER", erforderlich=False, standard="INBOX")

    html_liste = []

    LOGGER.info("Verbinde mit IMAP-Server %s:%s.", host, port)

    context = ssl.create_default_context()

    with imaplib.IMAP4_SSL(host, port, ssl_context=context) as imap:
        imap.login(benutzer, passwort)

        status, _ = imap.select(ordner, readonly=False)

        if status != "OK":
            raise RuntimeError(f"Der Ordner {ordner!r} konnte nicht geöffnet werden.")

        kriterium = "UNSEEN" if nur_ungelesen else "ALL"

        status, suchdaten = imap.search(None, kriterium)

        if status != "OK":
            raise RuntimeError("Die Gmail-Suche ist fehlgeschlagen.")

        mail_ids = suchdaten[0].split()

        if maximale_anzahl > 0:
            mail_ids = mail_ids[-maximale_anzahl:]

        passende_mails = 0

        for mail_id in mail_ids:
            # PEEK: Mail wird beim Lesen NICHT automatisch als gelesen markiert
            status, daten = imap.fetch(mail_id, "(BODY.PEEK[])")

            if status != "OK":
                continue

            raw_email = None

            for element in daten:
                if isinstance(element, tuple) and len(element) >= 2:
                    raw_email = element[1]
                    break

            if raw_email is None:
                continue

            message = email.message_from_bytes(raw_email)

            if not email_passt_zum_filter(message, config):
                continue

            passende_mails += 1

            LOGGER.debug("Verarbeite E-Mail: %s", dekodiere_header(message, "Subject"))

            html_liste.extend(extrahiere_html_teile(message))

            # Nur passende Kleinanzeigen-Mails als gelesen markieren
            if nur_ungelesen:
                imap.store(mail_id, "+FLAGS", "\\Seen")

        LOGGER.info("%s passende Kleinanzeigen-Mail(s) verarbeitet.", passende_mails)

    return html_liste


# ==========================================================
# ANZEIGEN AUS HTML LESEN
# ==========================================================

def titel_aus_link(link) -> str:
    titel = text_bereinigen(link.get_text(" ", strip=True))

    if titel:
        return titel

    bild = link.find("img", alt=True)

    if bild:
        return text_bereinigen(bild.get("alt", ""))

    return text_bereinigen(link.get("aria-label", ""))


def umgebender_text(link) -> str:
    block = link

    for _ in range(8):
        if block.parent is None:
            break

        block = block.parent

        text = text_bereinigen(block.get_text(" ", strip=True))

        if (
            "€" in text
            or "verschenken" in text.lower()
            or re.search(r"\b\d{5}\b", text)
        ):
            return text

    return text_bereinigen(link.get_text(" ", strip=True))


def preistext_aus_text(text: str) -> str:
    text_klein = text.lower()

    for begriff in ("zu verschenken", "verschenke", "kostenlos", "gratis"):
        if begriff in text_klein:
            return "Zu verschenken"

    match = PREIS_PATTERN.search(text)

    if match:
        preis_text = match.group(0)
        danach = text_klein[match.end(): match.end() + 20]

        if re.search(r"\bvb\b", danach):
            preis_text += " VB"

        return preis_text

    if re.search(r"\bvb\b", text_klein):
        return "VB"

    return ""


def ort_aus_text(text: str) -> str:
    match = PLZ_PATTERN.search(text)

    if not match:
        return ""

    plz = match.group(1)
    ort = text_bereinigen(match.group(2))

    for marker in (" Zu verschenken", " Versand", " VB", " €"):
        position = ort.find(marker)

        if position >= 0:
            ort = ort[:position].strip()

    return f"{plz} {ort}".strip()


def anzeigen_parsen(html_text: str) -> list:
    soup = BeautifulSoup(html_text, "html.parser")

    gefunden = {}

    for link in soup.find_all("a", href=True):
        href = html.unescape(str(link["href"]))

        match = ANZEIGE_LINK_PATTERN.search(href)

        if not match:
            continue

        raw_url = match.group(0)

        if not raw_url.startswith("http"):
            raw_url = "https://" + raw_url.lstrip("/")

        url = sichere_url(raw_url)
        anzeige_id = extrahiere_anzeigen_id(url)
        titel = titel_aus_link(link)
        block_text = umgebender_text(link)
        preis_text = preistext_aus_text(block_text)
        ort = ort_aus_text(block_text)

        bestehend = gefunden.get(anzeige_id)

        if bestehend is None:
            gefunden[anzeige_id] = Anzeige(
                id=anzeige_id,
                url=url,
                titel=titel,
                preis_text=preis_text,
                ort=ort,
                gefunden_am=aktuelle_zeit(),
            )
            continue

        if titel and len(titel) > len(bestehend.titel) and "€" not in titel:
            bestehend.titel = titel

        if preis_text and not bestehend.preis_text:
            bestehend.preis_text = preis_text

        if ort and not bestehend.ort:
            bestehend.ort = ort

    return [anzeige for anzeige in gefunden.values() if anzeige.titel]


# ==========================================================
# PREIS UND FILTER
# ==========================================================

def preis_lesen(anzeige: Anzeige) -> None:
    text = anzeige.preis_text.lower()

    anzeige.verschenken = any(
        begriff in text for begriff in ("verschenken", "kostenlos", "gratis")
    )

    anzeige.vb = bool(re.search(r"\bvb\b", text))

    if anzeige.verschenken:
        anzeige.preis_eur = 0.0
        return

    match = PREIS_PATTERN.search(anzeige.preis_text)

    if not match:
        anzeige.preis_eur = None
        return

    zahl = match.group(1).replace(".", "").replace(",", ".")

    try:
        anzeige.preis_eur = float(zahl)
    except ValueError:
        anzeige.preis_eur = None


def stichwort_passt(titel_klein: str, titel_woerter: set, stichwort: str) -> bool:
    stichwort = stichwort.strip().lower()

    if not stichwort:
        return False

    if " " in stichwort:
        return stichwort in titel_klein

    return stichwort in titel_woerter


def bewerten(anzeige: Anzeige, config: dict) -> bool:
    preis_lesen(anzeige)

    titel_klein = anzeige.titel.lower()
    titel_woerter = wortmenge(anzeige.titel)

    # 1. Ausschlusswörter
    for ausschluss in config.get("ausschluss", []):
        ausschluss = str(ausschluss).strip().lower()

        if not ausschluss:
            continue

        if " " in ausschluss:
            gefunden = ausschluss in titel_klein
        else:
            gefunden = ausschluss in titel_woerter

        if gefunden:
            LOGGER.debug("Ausgeschlossen wegen %r: %s", ausschluss, anzeige.titel)
            return False

    # 2. Preis
    maximaler_preis = float(config.get("max_preis_eur", 1.0))

    if anzeige.preis_eur is None:
        if not (anzeige.vb and config.get("vb_erlauben", False)):
            return False
    elif anzeige.preis_eur > maximaler_preis:
        return False

    # 3. Ort
    if not anzeige.ort and not config.get("ort_darf_fehlen", True):
        return False

    # 4. Kategorie
    beste_kategorie = ""
    bester_name = ""
    bestes_emoji = "📦"
    beste_prioritaet = 0

    for name, kategorie in config.get("kategorien", {}).items():
        prioritaet = int(kategorie.get("prioritaet", 1))

        passt = any(
            stichwort_passt(titel_klein, titel_woerter, str(stichwort))
            for stichwort in kategorie.get("stichwoerter", [])
        )

        if not passt:
            passt = any(
                str(stichwort).lower() in titel_woerter
                for stichwort in kategorie.get("exakte_stichwoerter", [])
            )

        if passt and prioritaet > beste_prioritaet:
            beste_kategorie = name
            bester_name = kategorie.get("anzeige_name", name.title())
            bestes_emoji = kategorie.get("emoji", "📦")
            beste_prioritaet = prioritaet

    if not beste_kategorie:
        return False

    anzeige.kategorie = beste_kategorie
    anzeige.kategorie_name = bester_name
    anzeige.emoji = bestes_emoji
    anzeige.prioritaet = beste_prioritaet

    if anzeige.verschenken:
        anzeige.prioritaet += 1

    return True


# ==========================================================
# NACHRICHTENVORSCHLAG
# ==========================================================

def nachricht_bauen(
    anzeige: Anzeige,
    config: dict,
    zufall: random.Random | None = None,
) -> str:
    vorlagen = config.get("vorlagen", [])

    if not vorlagen:
        raise RuntimeError("In config.yaml fehlen Nachrichtenvorlagen.")

    if zufall is None:
        zufall = random.Random()

    vorlage = zufall.choice(vorlagen)

    titel = anzeige.titel

    if len(titel) > 80:
        titel = titel[:77].rstrip() + "..."

    nachricht = vorlage.format(
        titel=titel,
        name=config.get("mein_name", ""),
        abholung=config.get("abholung_text", "zeitnah"),
    )

    return text_bereinigen(nachricht)


# ==========================================================
# SQLITE-DATENBANK
# ==========================================================

class Datenbank:
    def __init__(self, path: Path = DATABASE_PATH):
        self.verbindung = sqlite3.connect(path)
        self.verbindung.row_factory = sqlite3.Row

        self.verbindung.executescript(
            """
            CREATE TABLE IF NOT EXISTS anzeigen
            (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                titel TEXT NOT NULL,
                preis_text TEXT,
                preis_eur REAL,
                ort TEXT,
                kategorie TEXT,
                kategorie_name TEXT,
                emoji TEXT,
                prioritaet INTEGER,
                nachrichtenvorschlag TEXT,
                gefunden_am INTEGER NOT NULL,
                versendet_am INTEGER
            );

            CREATE TABLE IF NOT EXISTS status
            (
                schluessel TEXT PRIMARY KEY,
                wert TEXT
            );
            """
        )

        self.verbindung.commit()

    def schliessen(self) -> None:
        self.verbindung.close()

    def ist_neu(self, anzeige_id: str) -> bool:
        zeile = self.verbindung.execute(
            "SELECT 1 FROM anzeigen WHERE id = ?",
            (anzeige_id,),
        ).fetchone()

        return zeile is None

    def speichern(self, anzeige: Anzeige) -> bool:
        cursor = self.verbindung.execute(
            """
            INSERT OR IGNORE INTO anzeigen
            (
                id, url, titel, preis_text, preis_eur, ort,
                kategorie, kategorie_name, emoji, prioritaet,
                nachrichtenvorschlag, gefunden_am, versendet_am
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                anzeige.id,
                anzeige.url,
                anzeige.titel,
                anzeige.preis_text,
                anzeige.preis_eur,
                anzeige.ort,
                anzeige.kategorie,
                anzeige.kategorie_name,
                anzeige.emoji,
                anzeige.prioritaet,
                anzeige.nachrichtenvorschlag,
                anzeige.gefunden_am or aktuelle_zeit(),
            ),
        )

        self.verbindung.commit()

        return cursor.rowcount > 0

    def offene_anzeigen(self, limit: int = 50) -> list:
        zeilen = self.verbindung.execute(
            """
            SELECT *
            FROM anzeigen
            WHERE versendet_am IS NULL
            ORDER BY prioritaet DESC, preis_eur ASC, gefunden_am ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        ergebnis = []

        for zeile in zeilen:
            ergebnis.append(
                Anzeige(
                    id=zeile["id"],
                    url=zeile["url"],
                    titel=zeile["titel"],
                    preis_text=zeile["preis_text"] or "",
                    preis_eur=zeile["preis_eur"],
                    ort=zeile["ort"] or "",
                    kategorie=zeile["kategorie"] or "",
                    kategorie_name=zeile["kategorie_name"] or "",
                    emoji=zeile["emoji"] or "📦",
                    prioritaet=zeile["prioritaet"] or 0,
                    nachrichtenvorschlag=zeile["nachrichtenvorschlag"] or "",
                    gefunden_am=zeile["gefunden_am"],
                )
            )

        return ergebnis

    def als_versendet_markieren(self, anzeigen: list) -> None:
        jetzt = aktuelle_zeit()

        self.verbindung.executemany(
            "UPDATE anzeigen SET versendet_am = ? WHERE id = ?",
            [(jetzt, anzeige.id) for anzeige in anzeigen],
        )

        self.verbindung.commit()

    def offene_anzahl(self) -> int:
        zeile = self.verbindung.execute(
            "SELECT COUNT(*) AS anzahl FROM anzeigen WHERE versendet_am IS NULL"
        ).fetchone()

        return int(zeile["anzahl"])

    def status_lesen(self, schluessel: str, standard: str = "") -> str:
        zeile = self.verbindung.execute(
            "SELECT wert FROM status WHERE schluessel = ?",
            (schluessel,),
        ).fetchone()

        if zeile:
            return str(zeile["wert"])

        return standard

    def status_schreiben(self, schluessel: str, wert: str) -> None:
        self.verbindung.execute(
            """
            INSERT INTO status (schluessel, wert)
            VALUES (?, ?)
            ON CONFLICT(schluessel) DO UPDATE SET wert = excluded.wert
            """,
            (schluessel, wert),
        )

        self.verbindung.commit()

    def letzte_anzeigen(self, limit: int = 20):
        return self.verbindung.execute(
            "SELECT * FROM anzeigen ORDER BY gefunden_am DESC LIMIT ?",
            (limit,),
        ).fetchall()


# ==========================================================
# TREFFER SPEICHERN
# ==========================================================

def treffer_speichern(html_liste: list, config: dict, datenbank: Datenbank) -> list:
    neue_treffer = []
    gesehen = set()

    for html_text in html_liste:
        anzeigen = anzeigen_parsen(html_text)

        LOGGER.debug("%s Anzeige(n) erkannt.", len(anzeigen))

        for anzeige in anzeigen:
            if anzeige.id in gesehen:
                continue

            gesehen.add(anzeige.id)

            if not datenbank.ist_neu(anzeige.id):
                continue

            if not bewerten(anzeige, config):
                continue

            anzeige.nachrichtenvorschlag = nachricht_bauen(anzeige, config)

            if datenbank.speichern(anzeige):
                neue_treffer.append(anzeige)

    def sortier_schluessel(eintrag: Anzeige):
        preis = eintrag.preis_eur if eintrag.preis_eur is not None else 999999
        return (-eintrag.prioritaet, preis, eintrag.titel.lower())

    neue_treffer.sort(key=sortier_schluessel)

    LOGGER.info("%s neue Treffer gespeichert.", len(neue_treffer))

    return neue_treffer


# ==========================================================
# BERICHTE
# ==========================================================

def gruppieren(anzeigen: list) -> dict:
    gruppen = {}

    for anzeige in anzeigen:
        name = anzeige.kategorie_name or "Sonstiges"
        gruppen.setdefault(name, []).append(anzeige)

    return gruppen


def erstelle_textbericht(anzeigen: list, config: dict) -> str:
    region = config.get("suchregion", {})

    zeilen = [
        "SCHNÄPPCHEN-RADAR",
        "=" * 70,
        "",
        f"{len(anzeigen)} neue Treffer",
        f"Region: {region.get('ort', 'Kelsterbach')} + {region.get('radius_km', 20)} km",
        "",
    ]

    for name, gruppe in gruppieren(anzeigen).items():
        zeilen.append(name.upper())
        zeilen.append("-" * 70)

        for anzeige in gruppe:
            zeilen.append(anzeige.titel)
            zeilen.append("Preis: " + (anzeige.preis_text or "unbekannt"))
            zeilen.append("Ort: " + (anzeige.ort or "siehe Anzeige"))
            zeilen.append("Link: " + anzeige.url)
            zeilen.append("Nachrichtenvorschlag:")
            zeilen.append(anzeige.nachrichtenvorschlag)
            zeilen.append("")

    return "\n".join(zeilen)


def erstelle_htmlbericht(anzeigen: list, config: dict) -> str:
    region = config.get("suchregion", {})

    bereiche = []

    for name, gruppe in gruppieren(anzeigen).items():
        emoji = gruppe[0].emoji if gruppe else "📦"
        karten = []

        for anzeige in gruppe:
            titel = html.escape(anzeige.titel)
            preis = html.escape(anzeige.preis_text or "unbekannt")
            ort = html.escape(anzeige.ort or "siehe Anzeige")
            url = html.escape(anzeige.url, quote=True)
            vorschlag = html.escape(anzeige.nachrichtenvorschlag)
            sterne = "★" * max(1, anzeige.prioritaet)

            karten.append(
                f"""
                <div style="background:#ffffff;border:1px solid #d1d5db;border-radius:12px;padding:18px;margin-bottom:16px;">
                    <h3 style="margin-top:0;color:#111827;">{titel}</h3>
                    <p>
                        <strong>Preis:</strong> {preis}<br>
                        <strong>Ort:</strong> {ort}<br>
                        <strong>Priorität:</strong>
                        <span style="color:#d97706;">{sterne}</span>
                    </p>
                    <p>
                        <a href="{url}" style="display:inline-block;background:#0f766e;color:#ffffff;text-decoration:none;padding:10px 16px;border-radius:8px;font-weight:bold;">
                            Anzeige öffnen
                        </a>
                    </p>
                    <div style="background:#f3f4f6;padding:12px;border-radius:8px;line-height:1.5;">
                        <strong>Nachrichtenvorschlag:</strong><br>
                        {vorschlag}
                    </div>
                </div>
                """
            )

        bereiche.append(
            f"""
            <h2 style="border-bottom:2px solid #0f766e;padding-bottom:8px;">
                {emoji} {html.escape(name)} ({len(gruppe)})
            </h2>
            {''.join(karten)}
            """
        )

    ort = html.escape(str(region.get("ort", "Kelsterbach")))
    radius = html.escape(str(region.get("radius_km", 20)))

    return f"""
    <!DOCTYPE html>
    <html lang="de">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Schnäppchen-Radar</title>
    </head>
    <body style="margin:0;padding:0;background:#eef2f3;font-family:Arial,sans-serif;">
        <div style="max-width:760px;margin:0 auto;padding:20px;">
            <div style="background:#0f766e;color:#ffffff;padding:24px;border-radius:14px;">
                <h1 style="margin:0 0 10px 0;">Schnäppchen-Radar</h1>
                <p style="margin:0;">{len(anzeigen)} neue Treffer in {ort} + {radius} km</p>
            </div>
            {''.join(bereiche)}
            <p style="background:#ffffff;padding:14px;border-radius:8px;color:#4b5563;font-size:13px;">
                Das Programm sendet keine Nachrichten automatisch an Verkäufer.
                Prüfe die Anzeige und sende den vorgeschlagenen Text selbst über Kleinanzeigen.
            </p>
        </div>
    </body>
    </html>
    """


# ==========================================================
# E-MAIL SENDEN
# ==========================================================

def zusammenfassung_senden(anzeigen: list, config: dict) -> None:
    if not anzeigen and config.get("zusammenfassung_nur_wenn_treffer", True):
        LOGGER.info("Keine Treffer. Keine E-Mail versendet.")
        return

    host = env_wert("SMTP_HOST")
    port = int(env_wert("SMTP_PORT", erforderlich=False, standard="587"))
    benutzer = env_wert("SMTP_USER")
    passwort = env_wert("SMTP_PASSWORD")
    empfaenger = env_wert("REPORT_EMAIL")

    absender_name = env_wert(
        "SMTP_FROM_NAME", erforderlich=False, standard="Schnaeppchen-Radar"
    )
    absender_email = env_wert(
        "SMTP_FROM_EMAIL", erforderlich=False, standard=benutzer
    )

    nachricht = EmailMessage()
    nachricht["Subject"] = f"🛒 Schnäppchen-Radar: {len(anzeigen)} neue Treffer"
    nachricht["From"] = f"{absender_name} <{absender_email}>"
    nachricht["To"] = empfaenger

    nachricht.set_content(erstelle_textbericht(anzeigen, config))
    nachricht.add_alternative(erstelle_htmlbericht(anzeigen, config), subtype="html")

    context = ssl.create_default_context()

    LOGGER.info("Sende %s Treffer an %s.", len(anzeigen), empfaenger)

    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as smtp:
            smtp.login(benutzer, passwort)
            smtp.send_message(nachricht)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(benutzer, passwort)
            smtp.send_message(nachricht)

    LOGGER.info("E-Mail erfolgreich versendet.")


def versand_faellig(datenbank: Datenbank, config: dict, sofort: bool) -> bool:
    if sofort:
        return True

    intervall = int(config.get("zusammenfassung_alle_minuten", 15))

    try:
        letzter_versand = int(datenbank.status_lesen("letzter_versand", "0"))
    except ValueError:
        letzter_versand = 0

    return (aktuelle_zeit() - letzter_versand) >= intervall * 60


def offene_zusammenfassung_senden(
    datenbank: Datenbank,
    config: dict,
    sofort: bool = False,
) -> int:
    if not versand_faellig(datenbank, config, sofort):
        LOGGER.info("Der Versandzeitpunkt ist noch nicht erreicht.")
        return 0

    anzeigen = datenbank.offene_anzeigen(
        limit=int(config.get("max_anzeigen_pro_mail", 50))
    )

    if not anzeigen:
        LOGGER.info("Keine offenen Treffer vorhanden.")
        return 0

    zusammenfassung_senden(anzeigen, config)

    datenbank.als_versendet_markieren(anzeigen)
    datenbank.status_schreiben("letzter_versand", str(aktuelle_zeit()))

    return len(anzeigen)


def test_email_senden(config: dict) -> None:
    beispiele = [
        Anzeige(
            id="test-1",
            url="https://www.kleinanzeigen.de/",
            titel="IKEA Kallax Regal weiß",
            preis_text="Zu verschenken",
            preis_eur=0.0,
            ort="65451 Kelsterbach",
            verschenken=True,
            kategorie="ikea",
            kategorie_name="IKEA und Möbel",
            emoji="🪑",
            prioritaet=3,
            gefunden_am=aktuelle_zeit(),
        ),
        Anzeige(
            id="test-2",
            url="https://www.kleinanzeigen.de/",
            titel="PS4 Spielepaket",
            preis_text="1 €",
            preis_eur=1.0,
            ort="65428 Rüsselsheim",
            kategorie="gaming",
            kategorie_name="Gaming",
            emoji="🎮",
            prioritaet=3,
            gefunden_am=aktuelle_zeit(),
        ),
    ]

    for anzeige in beispiele:
        anzeige.nachrichtenvorschlag = nachricht_bauen(anzeige, config)

    zusammenfassung_senden(beispiele, config)


# ==========================================================
# KOMPLETTER PRUEFLAUF
# ==========================================================

def einmal_pruefen(
    config: dict,
    datenbank: Datenbank,
    nur_ungelesen: bool = True,
    sofort_senden: bool = False,
) -> tuple:
    mails = mail_holen(config, nur_ungelesen=nur_ungelesen)
    neue_treffer = treffer_speichern(mails, config, datenbank)
    versendet = offene_zusammenfassung_senden(datenbank, config, sofort=sofort_senden)

    return len(neue_treffer), versendet