"""The FAA registry import: ref_airframes at registry rank, claims and
events on observed airframes, no personal data, idempotent."""
import gzip
import io
import json
import zipfile

from sqlalchemy import select

from app import airframe_history, faa
from app.refdata_models import Airframe, AirframeClaim, AirframeEvent, \
    AirframeSpell, RefAirframe

MASTER_HEAD = ("N-NUMBER,SERIAL NUMBER,MFR MDL CODE,ENG MFR MDL,YEAR MFR,"
               "TYPE REGISTRANT,NAME,STREET,STREET2,CITY,STATE,ZIP CODE,"
               "REGION,COUNTY,COUNTRY,LAST ACTION DATE,CERT ISSUE DATE,"
               "CERTIFICATION,TYPE AIRCRAFT,TYPE ENGINE,STATUS CODE,"
               "MODE S CODE,FRACT OWNER,AIR WORTH DATE,OTHER NAMES(1),"
               "OTHER NAMES(2),OTHER NAMES(3),OTHER NAMES(4),"
               "OTHER NAMES(5),EXPIRATION DATE,UNIQUE ID,KIT MFR, KIT MODEL,"
               "MODE S CODE HEX,\n")


def _master_row(n, serial, year, registrant, name, cert, hex_id, aw):
    cols = [n, serial, "1384817", "52037", year, registrant, name,
            "1 MAIN ST", "", "FORT WORTH", "TX", "76155", "2", "439", "US",
            "20240101", cert, "1T", "5", "5", "V", "51234567", "", aw,
            "", "", "", "", "", "20290101", "00000001", "", "", hex_id, ""]
    return ",".join(cols) + "\n"


def _zip(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("MASTER.txt", "﻿" + MASTER_HEAD
                    + _master_row("957NN  ", "30095     ", "1999", "3",
                                  "AMERICAN AIRLINES INC", "20131107",
                                  "AD48F3    ", "19990603")
                    + _master_row("12345  ", "172-1     ", "1978", "1",
                                  "DOE JANE", "20200101", "A0B1C2    ", ""))
        zf.writestr("ACFTREF.txt", "﻿CODE,MFR,MODEL,\n"
                    "1384817,BOEING                        ,737-823  ,\n")
        zf.writestr("ENGINE.txt", "﻿CODE,MFR,MODEL,\n"
                    "52037,CFM INTL  ,CFM56-7B26 ,\n")
    path = tmp_path / "ReleasableAircraft.zip"
    path.write_bytes(buf.getvalue())
    return str(path)


def _observe(sm, tmp_path, hexes):
    doc = {"evidence_from": "2025-08-28", "airframes": {
        h: {"first": "2025-09-01", "last": "2026-09-20", "n": 50,
            "type": "B738", "regs": [], "ops": [], "gaps": []}
        for h in hexes}}
    path = tmp_path / "h.json.gz"
    path.write_bytes(gzip.compress(json.dumps(doc).encode()))
    with sm() as session:
        airframe_history.ingest_history(session, str(path))
        session.commit()


def test_faa_fills_the_registry_and_the_record(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    _observe(sm, tmp_path, ["ad48f3"])
    path = _zip(tmp_path)
    with sm() as session:
        assert faa.ingest_faa(session, path) == 2
        session.commit()
    with sm() as session:
        ref = session.get(RefAirframe, "ad48f3")
        assert (ref.registration, ref.year, ref.source) == \
            ("N957NN", 1999, "registry")
        assert session.get(RefAirframe, "a0b1c2").registration == "N12345"
        frame_id = session.execute(select(AirframeSpell.airframe_id).where(
            AirframeSpell.value == "ad48f3")).scalar_one()
        frame = session.get(Airframe, frame_id)
        assert (frame.msn, frame.built_year, frame.manufacturer) == \
            ("30095", 1999, "BOEING")
        claims = {c.field: c.value for c in session.execute(
            select(AirframeClaim)).scalars()}
        assert claims["model"] == "737-823"
        assert claims["engine"] == "CFM INTL CFM56-7B26"
        assert claims["owner"] == "AMERICAN AIRLINES INC"
        assert claims["certificate_date"] == "2013-11-07"
        # never observed: no record, and a private owner never lands
        assert session.execute(select(AirframeSpell).where(
            AirframeSpell.value == "a0b1c2")).first() is None
        assert "DOE JANE" not in {c.value for c in session.execute(
            select(AirframeClaim)).scalars()}
        kinds = sorted((e.kind, e.at.date().isoformat()) for e in
                       session.execute(select(AirframeEvent)).scalars())
        assert kinds == [("airworthiness", "1999-06-03"),
                         ("registered", "2013-11-07")]
        n_claims = session.query(AirframeClaim).count()

    # weekly re-import: nothing duplicated
    with sm() as session:
        faa.ingest_faa(session, path)
        session.commit()
        assert session.query(AirframeClaim).count() == n_claims
        assert session.query(AirframeEvent).count() == 2

    body = client.get("/v1/airframes/ad48f3").json()
    assert body["history"]["msn"] == "30095"
    assert body["history"]["built_year"] == 1999
    reg = body["history"]["registry"]
    assert reg["model"] == "737-823" and reg["source"] == "faa"
    assert reg["certificate_date"] == "2013-11-07"
    assert reg["owner"] == "AMERICAN AIRLINES INC"
    assert body["country"] == "US"


def test_same_serial_on_another_airframe_is_reported_not_merged(
        ctx, tmp_path, capsys):
    client, app, sm, settings, readsb = ctx
    _observe(sm, tmp_path, ["ad48f3", "a11111"])
    with sm() as session:
        faa.ingest_faa(session, _zip(tmp_path))
        session.commit()
        other = session.execute(select(AirframeSpell.airframe_id).where(
            AirframeSpell.value == "a11111")).scalar_one()
        # the same aircraft as seen under an earlier hex, per an older import
        for field, value in (("manufacturer", "BOEING"),
                             ("model", "737-823"), ("msn", "30095")):
            session.add(AirframeClaim(airframe_id=other, field=field,
                                      value=value, source="faa",
                                      licence="public-domain"))
        session.commit()
    with sm() as session:
        faa.ingest_faa(session, _zip(tmp_path))
        session.commit()
        assert faa.same_aircraft_candidates(session) == [
            ("BOEING", "737-823", "30095")]
        assert session.query(Airframe).count() == 2      # nothing merged
    assert "left for review" in capsys.readouterr().out


def test_a_serial_is_not_an_identity_on_its_own(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    _observe(sm, tmp_path, ["ad48f3", "a11111"])
    with sm() as session:
        for f in session.execute(select(Airframe)).scalars():
            f.msn = "003"                     # Falcon 2000 and 2000EX
        session.commit()
        assert session.query(Airframe).filter(Airframe.msn == "003").count() == 2
