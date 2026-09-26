//! The lexicon: the small tables query understanding reads (airlines,
//! served airports, cities, aircraft families, types), written beside
//! each index generation as lexicon.json and held in memory with the
//! lookups built from it. The index answers "which documents"; the
//! lexicon answers "what did they mean".

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

use super::text::{phrase, words};

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Lexicon {
    /// the index generation (the snapshot's creation time, compact)
    pub generation: String,
    /// the snapshot's creation time, ISO 8601 UTC
    pub as_of: String,
    /// days of flight log behind flight counts, when the log was read
    pub window_days: Option<i64>,
    pub airlines: Vec<Airline>,
    pub airports: Vec<Airport>,
    pub cities: Vec<City>,
    pub families: Vec<Family>,
    pub types: Vec<Type>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Airline {
    pub icao: String,
    pub iata: Option<String>,
    pub name: String,
    pub flights: i64,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Airport {
    /// IATA, else the ICAO/registry ident: the id /v2 and the site use
    pub code: String,
    pub ident: String,
    pub name: Option<String>,
    pub city: Option<String>,
    pub traffic: i64,
    pub large: bool,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct City {
    pub name: String,
    pub names: Vec<String>,
    /// airport codes, main first; only those in the snapshot
    pub airports: Vec<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Family {
    pub name: String,
    pub names: Vec<String>,
    /// designators, most airframes first
    pub designators: Vec<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Type {
    pub designator: String,
    pub name: Option<String>,
    pub airframes: i64,
}

/// A row of a tab-separated table under data/search: the name, its
/// other names, the codes it stands for. `#` starts a comment line.
pub fn table(src: &str) -> Vec<(String, Vec<String>, Vec<String>)> {
    src.lines()
        .filter(|l| !l.trim().is_empty() && !l.starts_with('#'))
        .filter_map(|l| {
            let mut cols = l.split('\t');
            let name = cols.next()?.trim().to_string();
            let others = cols.next().unwrap_or("").split(',').map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).collect();
            let codes = cols.next().unwrap_or("").split_whitespace().map(str::to_string).collect();
            Some((name, others, codes))
        })
        .collect()
}

pub const CITIES: &str = include_str!("../../data/search/cities.tsv");
pub const FAMILIES: &str = include_str!("../../data/search/families.tsv");

/// Words an airline name may end with and still be meant by the words
/// before them: "Qatar" is Qatar Airways, "Thai" is Thai Airways
/// International, "Singapore" is Singapore Airlines.
const GENERIC: &[&str] = &["airlines", "airline", "airways", "air", "lines", "international", "aviation", "aero", "aerolineas"];

/// What a place in a query stands for.
#[derive(Clone, Debug, PartialEq)]
pub struct Place {
    /// as typed (folded)
    pub text: String,
    /// airport codes, the likeliest first
    pub airports: Vec<String>,
    /// code, city or airport
    pub how: &'static str,
}

/// The lexicon with its lookups.
pub struct Lookup {
    pub lex: Lexicon,
    airline_by_icao: HashMap<String, usize>,
    airlines_by_iata: HashMap<String, Vec<usize>>,
    /// folded airline name words -> airlines
    airline_names: Vec<(Vec<String>, usize)>,
    airport_by_code: HashMap<String, usize>,
    /// folded city name -> airports, main first
    places: HashMap<String, Vec<String>>,
    /// folded airport name words, for "Changi", "Heathrow"
    airport_words: Vec<(Vec<String>, usize)>,
    families: HashMap<String, usize>,
    type_by_designator: HashMap<String, usize>,
}

impl Lookup {
    pub fn new(lex: Lexicon) -> Lookup {
        let mut airline_by_icao = HashMap::new();
        let mut airlines_by_iata: HashMap<String, Vec<usize>> = HashMap::new();
        let mut airline_names = vec![];
        for (k, a) in lex.airlines.iter().enumerate() {
            airline_by_icao.insert(a.icao.clone(), k);
            if let Some(i) = &a.iata {
                airlines_by_iata.entry(i.clone()).or_default().push(k);
            }
            airline_names.push((words(&a.name), k));
        }
        for v in airlines_by_iata.values_mut() {
            v.sort_by_key(|&k| std::cmp::Reverse(lex.airlines[k].flights));
        }
        let mut airport_by_code = HashMap::new();
        let mut airport_words = vec![];
        let mut by_city: HashMap<String, Vec<usize>> = HashMap::new();
        for (k, a) in lex.airports.iter().enumerate() {
            airport_by_code.insert(a.code.clone(), k);
            airport_by_code.entry(a.ident.clone()).or_insert(k);
            if let Some(n) = &a.name {
                airport_words.push((words(n), k));
            }
            // a served airport answers for its municipality
            if a.traffic > 0 {
                if let Some(c) = &a.city {
                    by_city.entry(phrase(c)).or_default().push(k);
                }
            }
        }
        let mut places: HashMap<String, Vec<String>> = HashMap::new();
        for (city, ks) in by_city.iter_mut() {
            ks.sort_by_key(|&k| std::cmp::Reverse(lex.airports[k].traffic));
            places.insert(city.clone(), ks.iter().map(|&k| lex.airports[k].code.clone()).collect());
        }
        // the cities table outranks the registry's municipalities
        for c in &lex.cities {
            for n in std::iter::once(&c.name).chain(c.names.iter()) {
                places.insert(phrase(n), c.airports.clone());
            }
        }
        let mut families = HashMap::new();
        for (k, f) in lex.families.iter().enumerate() {
            for n in std::iter::once(&f.name).chain(f.names.iter()) {
                families.insert(phrase(n), k);
                families.insert(phrase(n).replace(' ', ""), k);
            }
        }
        let type_by_designator = lex.types.iter().enumerate().map(|(k, t)| (t.designator.clone(), k)).collect();
        Lookup { lex, airline_by_icao, airlines_by_iata, airline_names, airport_by_code, places, airport_words, families, type_by_designator }
    }

    pub fn airline(&self, icao: &str) -> Option<&Airline> {
        self.airline_by_icao.get(icao).map(|&k| &self.lex.airlines[k])
    }

    /// ICAO codes of the airlines holding an IATA code, busiest first.
    pub fn icao_for_iata(&self, iata: &str) -> Vec<String> {
        self.airlines_by_iata.get(iata).map(|v| v.iter().map(|&k| self.lex.airlines[k].icao.clone()).collect()).unwrap_or_default()
    }

    /// An airline named by these words: a code (ICAO, IATA), its whole
    /// name, the start of its name when only generic words follow
    /// ("Qatar" is Qatar Airways), else the start of the name of an
    /// airline that flies ("Cathay"). The busiest of several.
    pub fn airline_named(&self, ws: &[String]) -> Option<String> {
        if ws.is_empty() {
            return None;
        }
        if ws.len() == 1 {
            let code = ws[0].to_uppercase();
            if code.len() == 3 && self.airline_by_icao.contains_key(&code) && code.chars().all(|c| c.is_ascii_alphabetic()) {
                return Some(code);
            }
            if code.len() == 2 {
                if let Some(first) = self.icao_for_iata(&code).into_iter().next() {
                    return Some(first);
                }
            }
        }
        // (whole name or generic words after it, flights, airline)
        let mut best: Option<(bool, i64, usize)> = None;
        for (name, k) in &self.airline_names {
            if name.len() < ws.len() || name[..ws.len()] != *ws {
                continue;
            }
            let generic = name[ws.len()..].iter().all(|w| GENERIC.contains(&w.as_str()));
            let f = self.lex.airlines[*k].flights;
            // the first words of another name ("Cathay" of Cathay
            // Pacific) only for an airline that flies
            if !generic && f == 0 {
                continue;
            }
            if best.is_none_or(|b| (generic, f) > (b.0, b.1)) {
                best = Some((generic, f, *k));
            }
        }
        best.map(|(_, _, k)| self.lex.airlines[k].icao.clone())
    }

    /// A place named by these words: an airport code, a city (the
    /// cities table, else the municipality of airports the network
    /// sees flights leave), or the name of a large airport it sees
    /// ("Changi", "Heathrow").
    pub fn place(&self, ws: &[String]) -> Option<Place> {
        if ws.is_empty() {
            return None;
        }
        let text = ws.join(" ");
        if ws.len() == 1 {
            let code = ws[0].to_uppercase();
            if (3..=4).contains(&code.len()) {
                if let Some(&k) = self.airport_by_code.get(&code) {
                    return Some(Place { text, airports: vec![self.lex.airports[k].code.clone()], how: "code" });
                }
            }
        }
        if let Some(codes) = self.places.get(&text) {
            let airports: Vec<String> = codes.iter().filter(|c| self.airport_by_code.contains_key(*c)).cloned().collect();
            if !airports.is_empty() {
                return Some(Place { text, airports, how: "city" });
            }
        }
        let mut found: Vec<(i64, usize)> = self
            .airport_words
            .iter()
            .filter(|(name, k)| {
                let a = &self.lex.airports[*k];
                a.large && a.traffic > 0 && ws.iter().all(|w| name.contains(w) && !GENERIC_PLACE.contains(&w.as_str()))
            })
            .map(|(_, k)| (self.lex.airports[*k].traffic, *k))
            .collect();
        found.sort_by_key(|&(t, _)| std::cmp::Reverse(t));
        found.truncate(3);
        (!found.is_empty()).then(|| Place { text, airports: found.iter().map(|&(_, k)| self.lex.airports[k].code.clone()).collect(), how: "airport" })
    }

    /// A family or a designator named by these words.
    pub fn family(&self, ws: &[String]) -> Option<(String, Vec<String>)> {
        let text = ws.join(" ");
        if let Some(&k) = self.families.get(&text).or_else(|| self.families.get(&text.replace(' ', ""))) {
            let f = &self.lex.families[k];
            return Some((f.name.clone(), f.designators.clone()));
        }
        if ws.len() == 1 {
            let d = ws[0].to_uppercase();
            if self.type_by_designator.contains_key(&d) && d.chars().any(|c| c.is_ascii_digit()) {
                return Some((d.clone(), vec![d]));
            }
        }
        None
    }

    pub fn is_family_name(&self, s: &str) -> bool {
        self.families.contains_key(&phrase(s))
    }
}

/// Words of airport names that name no airport on their own.
const GENERIC_PLACE: &[&str] = &["airport", "international", "intl", "air", "base", "field", "regional", "municipal", "county", "city"];

#[cfg(test)]
pub mod tests {
    use super::*;

    pub fn sample() -> Lexicon {
        let al = |icao: &str, iata: Option<&str>, name: &str, flights: i64| Airline {
            icao: icao.into(),
            iata: iata.map(Into::into),
            name: name.into(),
            flights,
        };
        let ap = |code: &str, ident: &str, name: &str, city: &str, traffic: i64, large: bool| Airport {
            code: code.into(),
            ident: ident.into(),
            name: Some(name.into()),
            city: Some(city.into()),
            traffic,
            large,
        };
        let mut cities = vec![];
        for (name, others, codes) in table(CITIES) {
            cities.push(City { name, names: others, airports: codes });
        }
        let mut families = vec![];
        for (name, others, codes) in table(FAMILIES) {
            families.push(Family { name, names: others, designators: codes });
        }
        Lexicon {
            generation: "20260926T024000Z".into(),
            as_of: "2026-09-26T02:40:00Z".into(),
            window_days: Some(400),
            airlines: vec![
                al("SIA", Some("SQ"), "Singapore Airlines", 900),
                al("SQC", None, "Singapore Airlines Cargo", 3),
                al("TGW", Some("TR"), "Scoot", 400),
                al("BAW", Some("BA"), "British Airways", 2000),
                al("QTR", Some("QR"), "Qatar Airways", 800),
                al("QFA", Some("QF"), "Qantas", 700),
                al("UAE", Some("EK"), "Emirates", 1000),
                al("THA", Some("TG"), "Thai Airways International", 300),
                al("TAX", None, "Thai AirAsia", 100),
                al("IGO", Some("6E"), "IndiGo", 1500),
                al("AEE", Some("A3"), "Aegean Airlines", 200),
                al("CRK", Some("HX"), "Hong Kong Airlines", 300),
            ],
            airports: vec![
                ap("SIN", "WSSS", "Singapore Changi Airport", "Singapore", 9000, true),
                ap("WSAC", "WSAC", "Changi Air Base (East)", "Singapore", 0, false),
                ap("LHR", "EGLL", "London Heathrow Airport", "London", 20000, true),
                ap("LGW", "EGKK", "London Gatwick Airport", "London", 8000, true),
                ap("JFK", "KJFK", "John F Kennedy International Airport", "New York", 9000, true),
                ap("LGA", "KLGA", "LaGuardia Airport", "New York", 12000, true),
                ap("EWR", "KEWR", "Newark Liberty International Airport", "Newark", 9000, true),
                ap("HND", "RJTT", "Tokyo Haneda International Airport", "Tokyo", 15000, true),
                ap("NRT", "RJAA", "Narita International Airport", "Narita", 7000, true),
                ap("MUC", "EDDM", "Munich Airport", "Munich", 8000, true),
                ap("OPO", "LPPR", "Francisco de Sá Carneiro Airport", "Porto", 2000, true),
                ap("POA", "SBPA", "Salgado Filho International Airport", "Porto Alegre", 1500, true),
                ap("GRU", "SBGR", "São Paulo/Guarulhos International Airport", "São Paulo", 5000, true),
                ap("LIS", "LPPT", "Humberto Delgado Airport", "Lisbon", 4000, true),
                ap("HKG", "VHHH", "Hong Kong International Airport", "Hong Kong", 9000, true),
            ],
            cities,
            families,
            types: vec![
                Type { designator: "A388".into(), name: Some("Airbus A380-800".into()), airframes: 250 },
                Type { designator: "A359".into(), name: Some("Airbus A350-900".into()), airframes: 600 },
                Type { designator: "B77W".into(), name: Some("Boeing 777-300ER".into()), airframes: 800 },
            ],
        }
    }

    fn w(s: &str) -> Vec<String> {
        words(s)
    }

    #[test]
    fn airlines_by_code_name_and_name_start() {
        let l = Lookup::new(sample());
        assert_eq!(l.airline_named(&w("SQ")).as_deref(), Some("SIA"));
        assert_eq!(l.airline_named(&w("sia")).as_deref(), Some("SIA"));
        assert_eq!(l.airline_named(&w("Singapore")).as_deref(), Some("SIA"));
        assert_eq!(l.airline_named(&w("singapore airlines")).as_deref(), Some("SIA"));
        assert_eq!(l.airline_named(&w("Qatar")).as_deref(), Some("QTR"));
        assert_eq!(l.airline_named(&w("thai")).as_deref(), Some("THA"));
        assert_eq!(l.airline_named(&w("6E")).as_deref(), Some("IGO"));
        assert_eq!(l.airline_named(&w("London")), None);
        // the first words of a name: the busiest airline that flies
        assert_eq!(l.airline_named(&w("british")).as_deref(), Some("BAW"));
        assert_eq!(l.airline_named(&w("aegean airlines")).as_deref(), Some("AEE"));
        assert_eq!(l.airline_named(&w("air")), None);
    }

    #[test]
    fn places_by_code_city_table_municipality_and_airport_name() {
        let l = Lookup::new(sample());
        let p = |s: &str| l.place(&w(s)).map(|p| (p.airports, p.how));
        assert_eq!(p("SIN"), Some((vec!["SIN".to_string()], "code")));
        assert_eq!(p("egll"), Some((vec!["LHR".to_string()], "code")));
        // the table's order; codes the snapshot lacks are left out
        assert_eq!(p("New York"), Some((vec!["JFK".into(), "EWR".into(), "LGA".into()], "city")));
        assert_eq!(p("Tokyo"), Some((vec!["HND".into(), "NRT".into()], "city")));
        assert_eq!(p("München"), Some((vec!["MUC".into()], "city")));
        assert_eq!(p("sao paulo"), Some((vec!["GRU".into()], "city")));
        assert_eq!(p("Porto"), Some((vec!["OPO".into()], "city")));
        assert_eq!(p("Changi"), Some((vec!["SIN".into()], "airport")));
        assert_eq!(p("Heathrow"), Some((vec!["LHR".into()], "airport")));
        assert_eq!(p("airport"), None);
        assert_eq!(p("Scoot"), None);
    }

    #[test]
    fn families_and_designators() {
        let l = Lookup::new(sample());
        assert_eq!(l.family(&w("A380")).map(|f| f.1), Some(vec!["A388".to_string()]));
        assert_eq!(l.family(&w("Boeing 777")).map(|f| f.0), Some("777".to_string()));
        assert_eq!(l.family(&w("787")).map(|f| f.1[0].clone()), Some("B789".to_string()));
        assert_eq!(l.family(&w("737 max")).map(|f| f.0), Some("737 MAX".to_string()));
        assert_eq!(l.family(&w("737max")).map(|f| f.0), Some("737 MAX".to_string()));
        assert_eq!(l.family(&w("A359")).map(|f| f.1), Some(vec!["A359".to_string()]));
        assert_eq!(l.family(&w("A999")), None);
        assert!(l.is_family_name("777") && !l.is_family_name("322"));
    }
}
