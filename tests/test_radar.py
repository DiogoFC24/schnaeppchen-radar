import random
import sys

from pathlib import Path


# ==========================================================
# PROJEKTORDNER IN PYTHON-PFAD AUFNEHMEN
# ==========================================================

PROJEKTORDNER = (
    Path(__file__)
    .resolve()
    .parents[1]
)

sys.path.insert(
    0,
    str(PROJEKTORDNER),
)


# ==========================================================
# EIGENES MODUL IMPORTIEREN
# ==========================================================

import radar


# ==========================================================
# TESTDATEN LADEN
# ==========================================================

HTML = (
    PROJEKTORDNER
    / "tests"
    / "beispiel_mail.html"
).read_text(
    encoding="utf-8"
)


CONFIG = radar.lade_config(
    PROJEKTORDNER
    / "config.yaml"
)


# ==========================================================
# TEST 1
#
# Werden alle 6 Anzeigen aus dem HTML erkannt?
# ==========================================================

def test_parser():

    anzeigen = radar.anzeigen_parsen(
        HTML
    )

    assert len(anzeigen) == 6


    nach_id = {
        anzeige.id: anzeige
        for anzeige in anzeigen
    }


    # IKEA-Testanzeige prüfen

    kallax = nach_id[
        "3012345678"
    ]


    assert (
        kallax.titel
        == "IKEA Kallax Regal weiß"
    )


    assert (
        kallax.preis_text
        == "Zu verschenken"
    )


    # Trackingparameter sollen entfernt worden sein.

    assert (
        "utm_source"
        not in kallax.url
    )


# ==========================================================
# TEST 2
#
# Funktioniert die Preisumwandlung?
# ==========================================================

def test_preis():

    anzeige = radar.Anzeige(
        id="1",
        url="https://example.com",
        titel="Testanzeige",
        preis_text="1,00 €",
    )


    radar.preis_lesen(
        anzeige
    )


    assert (
        anzeige.preis_eur
        == 1.0
    )


# ==========================================================
# TEST 3
#
# Kostenlos muss als 0 Euro erkannt werden.
# ==========================================================

def test_kostenlos():

    anzeige = radar.Anzeige(
        id="2",
        url="https://example.com",
        titel="IKEA Kallax",
        preis_text="Zu verschenken",
    )


    radar.preis_lesen(
        anzeige
    )


    assert (
        anzeige.preis_eur
        == 0.0
    )


    assert (
        anzeige.verschenken
        is True
    )


# ==========================================================
# TEST 4
#
# Welche Anzeigen bleiben nach der Filterung übrig?
# ==========================================================

def test_filter():

    treffer = {}


    anzeigen = radar.anzeigen_parsen(
        HTML
    )


    for anzeige in anzeigen:

        passt = radar.bewerten(
            anzeige,
            CONFIG,
        )


        if passt:

            treffer[
                anzeige.titel
            ] = anzeige


    # Genau diese drei sollen übrig bleiben.

    erwartet = {

        "IKEA Kallax Regal weiß",

        "PS4 Spiele Paket FIFA GTA",

        "Xbox One Controller",

    }


    assert (
        set(treffer)
        == erwartet
    )


# ==========================================================
# TEST 5
#
# IKEA-Kategorisierung prüfen.
# ==========================================================

def test_ikea_kategorie():

    anzeige = radar.Anzeige(
        id="3",
        url="https://example.com",
        titel="IKEA Kallax Regal weiß",
        preis_text="Zu verschenken",
        ort="65451 Kelsterbach",
    )


    assert radar.bewerten(
        anzeige,
        CONFIG,
    )


    assert (
        anzeige.kategorie
        == "ikea"
    )


    assert (
        anzeige.kategorie_name
        == "IKEA und Möbel"
    )


    # IKEA:
    #
    # Grundpriorität = 2
    # kostenlos      = +1
    #
    # Gesamt         = 3

    assert (
        anzeige.prioritaet
        == 3
    )


# ==========================================================
# TEST 6
#
# Gaming erkennen.
# ==========================================================

def test_gaming_kategorie():

    anzeige = radar.Anzeige(
        id="4",
        url="https://example.com",
        titel="Xbox One Controller",
        preis_text="1 €",
        ort="65479 Raunheim",
    )


    assert radar.bewerten(
        anzeige,
        CONFIG,
    )


    assert (
        anzeige.kategorie
        == "gaming"
    )


    assert (
        anzeige.preis_eur
        == 1.0
    )


# ==========================================================
# TEST 7
#
# Defekte Artikel ausschließen.
# ==========================================================

def test_defekter_artikel():

    anzeige = radar.Anzeige(
        id="5",
        url="https://example.com",
        titel="Monitor 24 Zoll defekt",
        preis_text="Zu verschenken",
        ort="64546 Mörfelden",
    )


    assert (
        radar.bewerten(
            anzeige,
            CONFIG,
        )
        is False
    )


# ==========================================================
# TEST 8
#
# Zu teure Artikel ausschließen.
# ==========================================================

def test_zu_teuer():

    anzeige = radar.Anzeige(
        id="6",
        url="https://example.com",
        titel="Vinyl Sammlung 30 LPs",
        preis_text="25 € VB",
        ort="60311 Frankfurt",
    )


    assert (
        radar.bewerten(
            anzeige,
            CONFIG,
        )
        is False
    )


# ==========================================================
# TEST 9
#
# Kostenlos bedeutet NICHT automatisch interessant.
# ==========================================================

def test_uninteressanter_gratis_artikel():

    anzeige = radar.Anzeige(
        id="7",
        url="https://example.com",
        titel="Altes Sofa",
        preis_text="Zu verschenken",
        ort="65451 Kelsterbach",
    )


    assert (
        radar.bewerten(
            anzeige,
            CONFIG,
        )
        is False
    )


# ==========================================================
# TEST 10
#
# Nachrichtenvorschlag erzeugen.
# ==========================================================

def test_nachricht():

    anzeige = radar.Anzeige(
        id="8",
        url="https://example.com",
        titel="Xbox One Controller",
        preis_text="1 €",
    )


    text = radar.nachricht_bauen(
        anzeige,
        CONFIG,
        zufall=random.Random(1),
    )


    assert (
        "Xbox One Controller"
        in text
    )


    assert (
        "Diogo"
        in text
    )


    # Es sollen keine nicht ersetzten
    # Platzhalter übrig bleiben.

    assert (
        "{"
        not in text
    )


    assert (
        "}"
        not in text
    )


# ==========================================================
# TEST 11
#
# SQLite verhindert doppelte Anzeigen.
# ==========================================================

def test_datenbank(
    tmp_path
):

    datenbank = radar.Datenbank(
        tmp_path
        / "test.sqlite"
    )


    try:

        anzeige = radar.Anzeige(

            id="123456",

            url="https://example.com",

            titel="PS4 Spiel",

            preis_text="1 €",

            preis_eur=1.0,

            ort="65451 Kelsterbach",

            kategorie="gaming",

            kategorie_name="Gaming",

            emoji="🎮",

            prioritaet=3,

            nachrichtenvorschlag=(
                "Hallo, ist das noch verfügbar?"
            ),

            gefunden_am=(
                radar.aktuelle_zeit()
            ),

        )


        # Vor dem Speichern:
        # Anzeige ist neu.

        assert (
            datenbank.ist_neu(
                anzeige.id
            )
            is True
        )


        # Speichern funktioniert.

        assert (
            datenbank.speichern(
                anzeige
            )
            is True
        )


        # Jetzt ist sie bekannt.

        assert (
            datenbank.ist_neu(
                anzeige.id
            )
            is False
        )


        # Ein zweites Speichern soll
        # keinen neuen Datensatz erzeugen.

        assert (
            datenbank.speichern(
                anzeige
            )
            is False
        )


        assert (
            datenbank.offene_anzahl()
            == 1
        )


        offene = (
            datenbank.offene_anzeigen()
        )


        assert (
            len(offene)
            == 1
        )


        # Als verschickt markieren.

        datenbank.als_versendet_markieren(
            offene
        )


        assert (
            datenbank.offene_anzahl()
            == 0
        )


    finally:

        datenbank.schliessen()


# ==========================================================
# TEST 12
#
# Treffer einer kompletten Testmail verarbeiten.
# ==========================================================

def test_komplette_testmail(
    tmp_path
):

    datenbank = radar.Datenbank(
        tmp_path
        / "mail_test.sqlite"
    )


    try:

        treffer = (
            radar.treffer_speichern(

                [HTML],

                CONFIG,

                datenbank,

            )
        )


        # Erwartet:
        #
        # 1. Kallax
        # 2. PS4 Spiele
        # 3. Xbox Controller

        assert (
            len(treffer)
            == 3
        )


        assert (
            datenbank.offene_anzahl()
            == 3
        )


        # Dieselbe Mail noch einmal.
        #
        # Keine neuen Treffer,
        # da die IDs bereits gespeichert sind.

        zweiter_lauf = (
            radar.treffer_speichern(

                [HTML],

                CONFIG,

                datenbank,

            )
        )


        assert (
            len(zweiter_lauf)
            == 0
        )


        assert (
            datenbank.offene_anzahl()
            == 3
        )


    finally:

        datenbank.schliessen()