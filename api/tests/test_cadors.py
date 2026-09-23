"""CADORS occurrences attach only where a registration spell covers the
date: a reissued registration never inherits another aircraft's past."""
import datetime

from sqlalchemy import select

from app import cadors
from app.refdata_models import Airframe, AirframeEvent, AirframeSpell

OCC = ("cadorsnumber,occurrencetypedescriptione,occurrencetypedescriptionf,"
       "occurrencedate,occurrencetime,dayornight,fatalities,Injuries,"
       "aerodromeid,aerodromelocation,occurrencelocation,subdivision_enm,"
       "subdivision_fnm,tc_region_enm,tc_region_fnm,country_enm,country_fnm,"
       "worldareanamee,worldareanamef,aornumber,occurrence_class_type_elbl,"
       "occurrence_class_type_flbl,tsboccurrencenumber\n"
       "2026C1001,Incident,Incident,2026-03-04,1915 Z,Day,0E-18,0E-18,CYYC,"
       "CALGARY AB (CYYC),Calgary,Alberta,Alberta,Prairie,Prairie,Canada,"
       "Canada,North America,Am,1-V1,,,\n"
       "2011Q0288,Incident,Incident,2011-05-02,0602 Z,,0E-18,0E-18,CYUL,"
       "MONTREAL,Montreal,Quebec,Quebec,Q,Q,Canada,Canada,NA,NA,2-V1,,,\n"
       "2026A2002,Incident,Incident,2026-02-10,1200 Z,,0E-18,1.0000,CYYZ,"
       "TORONTO,Toronto,Ontario,Ontario,O,O,Canada,Canada,NA,NA,3-V1,,,\n")
EVENTS = ("cadorsnumber,event_name_enm,event_name_fnm\n"
          "2026C1001,Bird strike,Impact d'oiseau\n"
          "2026C1001,Return to departure aerodrome,Retour\n"
          "2011Q0288,ATM - operations,ATM\n")
AIRCRAFT_HEAD = ("cadorsnumber,aircraftnumber,aircraftregistration,"
                 "foreignaircraftregistration,flightnumber,flight_rule_enm,"
                 "flight_rule_fnm,Categorydescriptione,Categorydescriptionf,"
                 "Country_Enm,Country_Fnm,aircraft_make_name_nm,"
                 "aircraft_model_name_nm,aircraftyearbuilt,homebuiltaircraft,"
                 "engine_manufacturer_name_nm,engine_model_name_nm,"
                 "enginetypedescriptione,enginetypedescriptionf,"
                 "geartypedescriptione,geartypedescriptionf,phasenamee,"
                 "phasenamef,damagedescriptione,damagedescriptionf,operator,"
                 "operatortypedescriptione,operatortypedescriptionf,"
                 "operation_sector_elbl,operation_sector_flbl,owner\n")


def _ac(num, dom, foreign, flight, phase):
    return (f"{num},1.0,{dom},{foreign},{flight},,,Aeroplane,Avion,Canada,"
            f"Canada,BOEING,737,2015.0,No,CFM,LEAP,Turbo fan,T,Land,T,"
            f"{phase},{phase},No Damage,Aucun,SOME PERSON,Commercial,C,,,"
            f"SOME PERSON\n")


def _dir(tmp_path):
    (tmp_path / "CADORS_Occurrence_Information.csv").write_text(OCC)
    (tmp_path / "CADORS_Occurrence_Event_Information.csv").write_text(EVENTS)
    (tmp_path / "CADORS_Aircraft_Information.csv").write_text(
        AIRCRAFT_HEAD
        + _ac("2026C1001", "GJZT", "", "WJA123", "Climb")
        + _ac("2011Q0288", "GJZT", "", "JZA3601", "Approach")
        + _ac("2026A2002", "", "A6EDA", "UAE241", "Taxi"))
    return str(tmp_path)


def _frame(session, reg, first, last):
    frame = Airframe()
    session.add(frame)
    session.flush()
    session.add(AirframeSpell(airframe_id=frame.id, kind="registration",
                              value=reg, first_date=first, last_date=last,
                              source="observed"))
    return frame.id


def test_occurrences_attach_inside_the_registration_spell(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    d = datetime.date
    with sm() as session:
        westjet = _frame(session, "C-GJZT", d(2025, 9, 1), d(2026, 9, 20))
        emirates = _frame(session, "A6-EDA", d(2025, 9, 1), d(2026, 1, 20))
        session.commit()
        n = cadors.ingest_cadors(session, _dir(tmp_path))
        session.commit()
        assert n == 2
        events = {e.airframe_id: e for e in session.execute(
            select(AirframeEvent)).scalars()}
    ev = events[westjet]
    assert ev.kind == "occurrence" and ev.evidence_ref == "CADORS 2026C1001"
    assert ev.at.replace(tzinfo=None) == datetime.datetime(2026, 3, 4, 19, 15)
    assert ev.detail["events"] == ["Bird strike",
                                   "Return to departure aerodrome"]
    assert ev.detail["registration"] == "C-GJZT"
    assert ev.detail["phase"] == "Climb" and ev.detail["flight"] == "WJA123"
    assert "SOME PERSON" not in str(ev.detail)       # no owner, no operator
    # 2026-02-10 is 21 days after the spell's last day: inside the slack
    assert events[emirates].detail["injuries"] == 1
    # the 2011 report on C-GJZT is before any spell: never attached
    assert len(events) == 2
    with sm() as session:                            # re-import: nothing new
        assert cadors.ingest_cadors(session, _dir(tmp_path)) == 0
