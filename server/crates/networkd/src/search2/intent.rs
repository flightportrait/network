//! Query understanding: what a query may mean, likeliest first, before
//! anything is searched. Each reading names what to look up (a flight
//! number's callsigns, a route's airports, an airline and a family);
//! the engine tries them in order and keeps the first that finds
//! something, with plain text search beneath.

use super::lexicon::{Lookup, Place};
use super::text::{compact, words};

#[derive(Clone, Debug, PartialEq)]
pub enum Intent {
    /// a flight number: the callsigns and marketed numbers it may be
    Flight { number: String, callsigns: Vec<String> },
    /// a registration typed with a space or without its dash
    Registration { bare: String },
    Route { from: Place, to: Place },
    Fleet { airline: String, family: String, types: Vec<String> },
    AirlinePlace { airline: String, place: Place },
    Type { family: String, types: Vec<String> },
    City { place: Place },
}

impl Intent {
    pub fn kind(&self) -> &'static str {
        match self {
            Intent::Flight { .. } => "flight",
            Intent::Registration { .. } => "registration",
            Intent::Route { .. } => "route",
            Intent::Fleet { .. } => "fleet",
            Intent::AirlinePlace { .. } => "airline_place",
            Intent::Type { .. } => "type",
            Intent::City { .. } => "city",
        }
    }
}

/// "SQ322", "SQ 322", "SIA322", "6E1", "BA16A": (airline code, number).
/// An IATA code is two letters or digits with a letter among them, an
/// ICAO code three letters; the number one to four digits and perhaps
/// a letter.
pub fn flight_number(q: &str) -> Option<(String, String)> {
    let s: String = q.chars().filter(|c| *c != ' ').collect::<String>().to_uppercase();
    if !s.is_ascii() {
        return None;
    }
    let number_ok = |n: &str| {
        let digits = n.chars().take_while(|c| c.is_ascii_digit()).count();
        let rest = &n[digits..];
        (1..=4).contains(&digits) && (rest.is_empty() || (rest.len() == 1 && rest.chars().all(|c| c.is_ascii_uppercase())))
    };
    // the space, when typed, says where the code ends
    if let Some((code, number)) = q.trim().split_once(' ') {
        let code = code.to_uppercase();
        let ok = match code.len() {
            2 => code.chars().all(|c| c.is_ascii_alphanumeric()) && code.chars().any(|c| c.is_ascii_alphabetic()),
            3 => code.chars().all(|c| c.is_ascii_alphabetic()),
            _ => false,
        };
        return (ok && number_ok(&number.to_uppercase())).then(|| (code, number.to_uppercase()));
    }
    if s.len() >= 4 && s[..3].chars().all(|c| c.is_ascii_alphabetic()) && number_ok(&s[3..]) {
        return Some((s[..3].to_string(), s[3..].to_string()));
    }
    let two = s.get(..2)?;
    if two.chars().all(|c| c.is_ascii_alphanumeric()) && two.chars().any(|c| c.is_ascii_alphabetic()) && number_ok(&s[2..]) {
        return Some((two.to_string(), s[2..].to_string()));
    }
    None
}

/// Every reading of `q` (canonical: trimmed, single spaces, uppercase),
/// likeliest first.
pub fn parse(q: &str, lx: &Lookup) -> Vec<Intent> {
    let ws = words(q);

    // a flight number of an airline the lexicon knows, as typed and as
    // its airline's other code
    let mut flight = None;
    if let Some((code, number)) = flight_number(q) {
        let mut callsigns = vec![format!("{code}{number}")];
        let known = if code.len() == 2 {
            let icaos = lx.icao_for_iata(&code);
            let known = !icaos.is_empty();
            callsigns.extend(icaos.into_iter().take(3).map(|icao| format!("{icao}{number}")));
            known
        } else if let Some(a) = lx.airline(&code) {
            callsigns.extend(a.iata.iter().map(|iata| format!("{iata}{number}")));
            true
        } else {
            false
        };
        callsigns.dedup();
        if known {
            flight = Some(Intent::Flight { number: format!("{code}{number}"), callsigns });
        }
    }

    // a registration typed with a space ("9V SMA"): a nationality mark
    // of one or two characters, then the rest of the mark
    let mut registration = None;
    if let Some((mark, rest)) = q.split_once(' ') {
        if (1..=2).contains(&mark.len())
            && (2..=5).contains(&rest.len())
            && !rest.contains(' ')
            && mark.chars().chain(rest.chars()).all(|c| c.is_ascii_alphanumeric())
        {
            registration = Some(Intent::Registration { bare: compact(q) });
        }
    }

    // two places: a route. Two words with nothing between them that
    // are one place together ("Los Angeles", "Hong Kong") are that place
    let mut route = None;
    let joined = [" TO ", "FROM ", "-", ">", "\u{2192}", "\u{2013}", "\u{2014}"].iter().any(|c| q.contains(c));
    let one_place = lx.place(&ws).is_some();
    if let Some((a, b)) = crate::search::places(q).filter(|_| joined || !one_place) {
        if let (Some(from), Some(to)) = (lx.place(&words(&a)), lx.place(&words(&b))) {
            // "Tokyo Haneda" is one airport, not a route
            if !from.airports.iter().any(|x| to.airports.contains(x)) {
                route = Some(Intent::Route { from, to });
            }
        }
    }

    // an airline and a family, or an airline and a place, either order
    let mut fleet = None;
    let mut airline_place = None;
    for i in 1..ws.len() {
        let (l, r) = (&ws[..i], &ws[i..]);
        for (al, other) in [(l, r), (r, l)] {
            let Some(icao) = lx.airline_named(al) else { continue };
            let family = lx.family(other);
            if fleet.is_none() {
                if let Some((family, types)) = family.clone() {
                    fleet = Some(Intent::Fleet { airline: icao.clone(), family, types });
                }
            }
            // "Hong Kong" is a city, not Hong Kong Airlines at "Kong"
            if airline_place.is_none() && family.is_none() && !one_place {
                if let Some(place) = lx.place(other) {
                    airline_place = Some(Intent::AirlinePlace { airline: icao, place });
                }
            }
        }
    }

    let alone = lx.family(&ws).map(|(family, types)| Intent::Type { family, types });
    let city = lx.place(&ws).filter(|p| p.how == "city").map(|place| Intent::City { place });

    // "SQ 777" is a fleet before a flight number, "A380" a type
    let flight_later = ws.len() == 2 && lx.is_family_name(&ws[1]) || alone.is_some();
    let mut out = vec![];
    if !flight_later {
        out.extend(flight.take());
    }
    out.extend(route);
    out.extend(fleet);
    if alone.is_none() {
        out.extend(flight.take());
    }
    out.extend(registration);
    out.extend(airline_place);
    out.extend(alone);
    out.extend(flight);
    out.extend(city);
    out
}

#[cfg(test)]
mod tests {
    use super::super::lexicon::tests::sample;
    use super::*;

    fn kinds(q: &str) -> Vec<&'static str> {
        let l = Lookup::new(sample());
        parse(q, &l).iter().map(Intent::kind).collect()
    }

    fn first(q: &str) -> Intent {
        let l = Lookup::new(sample());
        parse(q, &l).into_iter().next().unwrap_or_else(|| panic!("no reading of {q}"))
    }

    #[test]
    fn flight_numbers_iata_icao_spaced() {
        let s = |a: &str, b: &str| Some((a.to_string(), b.to_string()));
        assert_eq!(flight_number("SQ322"), s("SQ", "322"));
        assert_eq!(flight_number("SQ 322"), s("SQ", "322"));
        assert_eq!(flight_number("SIA322"), s("SIA", "322"));
        assert_eq!(flight_number("SIA 322"), s("SIA", "322"));
        assert_eq!(flight_number("6E1"), s("6E", "1"));
        assert_eq!(flight_number("BA16A"), s("BA", "16A"));
        for q in ["SIN", "787", "9V-SMA", "SQ32222", "S 1", "SINGAPORE", "SQ 322 X", "BA16AB"] {
            assert_eq!(flight_number(q), None, "{q}");
        }
        assert_eq!(
            first("SQ 322"),
            Intent::Flight { number: "SQ322".into(), callsigns: vec!["SQ322".into(), "SIA322".into()] }
        );
        assert_eq!(
            first("SIA322"),
            Intent::Flight { number: "SIA322".into(), callsigns: vec!["SIA322".into(), "SQ322".into()] }
        );
    }

    #[test]
    fn routes_in_words_with_cities() {
        match first("NEW YORK TO LONDON") {
            Intent::Route { from, to } => {
                assert_eq!(from.airports, ["JFK", "EWR", "LGA"]);
                assert_eq!(to.airports, ["LHR", "LGW"]);
            }
            other => panic!("{other:?}"),
        }
        for q in ["SIN LHR", "SIN-LHR", "SINGAPORE TO LONDON", "CHANGI TO HEATHROW", "FLIGHTS TO LONDON FROM SINGAPORE"] {
            assert_eq!(kinds(q)[0], "route", "{q}");
        }
        match first("SAO PAULO TO LISBON") {
            Intent::Route { from, to } => assert_eq!((from.airports, to.airports), (vec!["GRU".into()], vec!["LIS".into()])),
            other => panic!("{other:?}"),
        }
        // one airport, two names: not a route
        assert!(!kinds("TOKYO HANEDA").contains(&"route"));
        // two words, one city
        assert_eq!(kinds("NEW YORK"), ["city"]);
        assert_eq!(kinds("SAO PAULO"), ["city"]);
        assert_eq!(kinds("HONG KONG"), ["city"]);
    }

    #[test]
    fn airline_with_a_family_or_a_place() {
        assert_eq!(first("SQ 777"), Intent::Fleet {
            airline: "SIA".into(),
            family: "777".into(),
            types: "B77W B772 B77L B773 B778 B779".split(' ').map(String::from).collect()
        });
        // the number still reads as a flight, after the fleet
        assert_eq!(kinds("SQ 777"), ["fleet", "flight", "registration"]);
        for (q, al, fam) in [("BA A380", "BAW", "A380"), ("QATAR A350", "QTR", "A350"), ("A380 QANTAS", "QFA", "A380"), ("SINGAPORE A350", "SIA", "A350"), ("SIA A359", "SIA", "A359")] {
            match first(q) {
                Intent::Fleet { airline, family, .. } => assert_eq!((airline.as_str(), family.as_str()), (al, fam), "{q}"),
                other => panic!("{q}: {other:?}"),
            }
        }
        match first("SINGAPORE AIRLINES LONDON") {
            Intent::AirlinePlace { airline, place } => assert_eq!((airline.as_str(), place.airports.len()), ("SIA", 2)),
            other => panic!("{other:?}"),
        }
        match first("SCOOT SINGAPORE") {
            Intent::AirlinePlace { airline, place } => assert_eq!((airline, place.airports), ("TGW".to_string(), vec!["SIN".to_string()])),
            other => panic!("{other:?}"),
        }
        // two places stay a route even when one is also an airline's name
        assert_eq!(kinds("SINGAPORE LONDON")[0], "route");
    }

    #[test]
    fn types_cities_and_registrations() {
        assert_eq!(first("A380"), Intent::Type { family: "A380".into(), types: vec!["A388".into()] });
        assert_eq!(kinds("BOEING 777"), ["type"]);
        match first("787") {
            Intent::Type { types, .. } => assert_eq!(types[0], "B789"),
            other => panic!("{other:?}"),
        }
        match first("TOKYO") {
            Intent::City { place } => assert_eq!(place.airports, ["HND", "NRT"]),
            other => panic!("{other:?}"),
        }
        assert_eq!(kinds("MÜNCHEN"), ["city"]);
        assert_eq!(kinds("9V SMA"), ["registration"]);
        // words that are no place, airline or type: plain text search
        assert!(kinds("CHARLES DE GAULLE").is_empty());
        assert!(kinds("QANTSA").is_empty());
        // a code that is no airline's is no flight number
        assert!(kinds("N12005").is_empty());
        // a family's name is a type before it is a flight number
        assert_eq!(kinds("A380"), ["type", "flight"]);
    }
}
