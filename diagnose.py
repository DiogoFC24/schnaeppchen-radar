"""
diagnose.py - zeigt, was der Radar in den Kleinanzeigen-Mails sieht.

Aendert NICHTS: Das Postfach wird nur lesend geoeffnet, keine Mail wird als
gelesen markiert, nichts wird gespeichert oder gesendet.
Start:  python diagnose.py
"""
from __future__ import annotations

import email
import html
import imaplib
import re
import ssl
from collections import Counter
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

import radar

ANZAHL_MAILS = 5       # so viele passende Mails werden angezeigt (neueste zuerst)
MAX_GEPRUEFT = 300     # so viele Mails werden hoechstens durchsucht


def maskiere(text: str) -> str:
    """E-Mail-Adressen unkenntlich machen, weil das Log oeffentlich sein kann."""
    return re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "***@***", text)


def sichtbarer_text(html_text: str, laenge: int = 240) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = radar.text_bereinigen(soup.get_text(" ", strip=True))
    return maskiere(text)[:laenge]


def link_statistik(html_text: str) -> dict:
    soup = BeautifulSoup(html_text, "html.parser")
    hosts = Counter()
    gesamt = direkt = versteckt = 0

    for a in soup.find_all("a", href=True):
        gesamt += 1
        href = html.unescape(str(a["href"]))
        host = urlsplit(href).netloc.lower() or "(ohne Host)"
        hosts[host] += 1

        if radar.ANZEIGE_LINK_PATTERN.search(href):
            direkt += 1
        elif radar.ANZEIGE_LINK_PATTERN.search(unquote(unquote(href))):
            versteckt += 1

    return {
        "gesamt": gesamt,
        "direkt": direkt,
        "versteckt": versteckt,
        "hosts": hosts.most_common(6),
    }


def urteil(anzeige: radar.Anzeige, config: dict) -> str:
    """Sagt, warum eine Anzeige Treffer ist oder verworfen wird."""
    if radar.bewerten(anzeige, config):
        return f"TREFFER ({anzeige.kategorie_name})"

    titel_klein = anzeige.titel.lower()
    woerter = radar.wortmenge(anzeige.titel)

    for ausschluss in config.get("ausschluss", []):
        a = str(ausschluss).strip().lower()
        if not a:
            continue
        gefunden = (a in titel_klein) if " " in a else (a in woerter)
        if gefunden:
            return f"verworfen: Ausschlusswort '{a}'"

    limit = float(config.get("max_preis_eur", 1.0))

    if anzeige.preis_eur is None:
        if not (anzeige.vb and config.get("vb_erlauben", False)):
            return "verworfen: kein Preis erkannt"
    elif anzeige.preis_eur > limit:
        return f"verworfen: Preis {anzeige.preis_eur:g} EUR liegt ueber {limit:g} EUR"

    if not anzeige.ort and not config.get("ort_darf_fehlen", True):
        return "verworfen: kein Ort erkannt"

    return "verworfen: keine Kategorie passt zum Titel"


def mails_lesen(config: dict) -> tuple:
    host = radar.env_wert("IMAP_HOST")
    port = int(radar.env_wert("IMAP_PORT", erforderlich=False, standard="993"))
    benutzer = radar.env_wert("IMAP_USER")
    passwort = radar.env_wert("IMAP_PASSWORD")
    ordner = radar.env_wert("IMAP_FOLDER", erforderlich=False, standard="INBOX")

    passende = []
    geprueft = 0
    gesamt = 0

    context = ssl.create_default_context()

    with imaplib.IMAP4_SSL(host, port, ssl_context=context) as imap:
        imap.login(benutzer, passwort)
        imap.select(ordner, readonly=True)   # nur lesen

        status, daten = imap.search(None, "ALL")
        ids = daten[0].split() if status == "OK" else []
        gesamt = len(ids)

        for mail_id in reversed(ids[-MAX_GEPRUEFT:]):
            geprueft += 1
            status, antwort = imap.fetch(mail_id, "(BODY.PEEK[])")

            if status != "OK":
                continue

            roh = None
            for element in antwort:
                if isinstance(element, tuple) and len(element) >= 2:
                    roh = element[1]
                    break

            if roh is None:
                continue

            message = email.message_from_bytes(roh)

            if radar.email_passt_zum_filter(message, config):
                passende.append(message)

            if len(passende) >= ANZAHL_MAILS:
                break

    return passende, geprueft, gesamt


def bericht(message, nummer: int, config: dict) -> None:
    betreff = maskiere(radar.dekodiere_header(message, "Subject"))
    absender = radar.dekodiere_header(message, "From")
    absender_domain = absender.split("@")[-1].strip(" >") if "@" in absender else "?"
    datum = message.get("Date", "?")

    print()
    print("=" * 72)
    print(f"Mail {nummer}")
    print("=" * 72)
    print("Betreff :", betreff)
    print("Absender: ...@" + absender_domain)
    print("Datum   :", datum)

    teile = radar.extrahiere_html_teile(message)

    if not teile:
        print("Inhalt  : kein lesbarer HTML- oder Text-Teil gefunden")
        return

    for index, html_text in enumerate(teile, start=1):
        if len(teile) > 1:
            print(f"--- Teil {index} ---")

        print("Text    :", sichtbarer_text(html_text))

        stat = link_statistik(html_text)
        print(
            f"Links   : {stat['gesamt']} gesamt, "
            f"{stat['direkt']} direkt auf /s-anzeige/, "
            f"{stat['versteckt']} nur nach URL-Dekodierung"
        )

        if stat["hosts"]:
            print("Hosts   :", ", ".join(f"{h} ({n})" for h, n in stat["hosts"]))

        anzeigen = radar.anzeigen_parsen(html_text)
        print(f"Anzeigen vom Parser erkannt: {len(anzeigen)}")

        for anzeige in anzeigen:
            ort = anzeige.ort or "-"
            preis = anzeige.preis_text or "-"
            print(f"  - {anzeige.titel[:60]!r} | Preis: {preis} | Ort: {ort}")
            print(f"      -> {urteil(anzeige, config)}")


def main() -> None:
    config = radar.lade_config()

    print("DIAGNOSE (aendert nichts am Postfach)")
    print(
        f"Limit: {config.get('max_preis_eur', 1.0)} EUR, "
        f"Absenderfilter: {config.get('email_filter', {}).get('absender_enthaelt')}"
    )

    mails, geprueft, gesamt = mails_lesen(config)

    print(f"Posteingang: {gesamt} Mails insgesamt, {geprueft} durchsucht")
    print(f"Davon passen zum Absenderfilter (zeige bis zu {ANZAHL_MAILS}): {len(mails)}")

    if not mails:
        print()
        print("Keine passende Mail im Posteingang gefunden.")
        print("Pruefe: Ist die Mail im Spam-Ordner? Kommt sie von einer anderen Absenderadresse?")
        return

    for nummer, message in enumerate(mails, start=1):
        bericht(message, nummer, config)


if __name__ == "__main__":
    main()
