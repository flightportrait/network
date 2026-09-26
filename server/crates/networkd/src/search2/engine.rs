//! One generation of the index, open read-only (memory-mapped), and the
//! search over it: the readings of the query first (intent.rs), then the
//! exact codes, then text, all ranked on one scale.
//!
//! The scale (see `rank`): an exact code 1000; the likeliest reading's
//! results 900, each further reading 40 less; words matched whole 600,
//! a word still being typed 500, a typo 470 (one edit) or 380 (two);
//! some of the words 150-300. Within a class, the popularity prior
//! (log of flights, departures or airframes) and BM25 order things, and
//! never lift a result into the class above. Near misses are looked for
//! only when nothing matched whole, so a typo never outranks a whole
//! match of another kind (Qantsa is Qantas, not an airport near Santa
//! Ana).

use std::collections::HashMap;
use std::path::Path;

use serde_json::{json, Map, Value};
use tantivy::collector::TopDocs;
use tantivy::query::{BooleanQuery, FuzzyTermQuery, Occur, Query, TermQuery, TermSetQuery};
use tantivy::schema::{IndexRecordOption, Value as _};
use tantivy::{DocAddress, Index, IndexReader, ReloadPolicy, Searcher, TantivyDocument, Term};

use super::intent::{parse, Intent};
use super::lexicon::{Lexicon, Lookup, Place};
use super::schema::{fields, Fields, KINDS};
use super::text::{allowed_edits, compact, distance, words};

pub struct Engine {
    pub generation: String,
    pub as_of: String,
    reader: IndexReader,
    f: Fields,
    pub lx: Lookup,
}

/// What was asked, as the route validated it.
pub struct Ask<'a> {
    /// canonical: trimmed, single spaces, uppercase
    pub q: &'a str,
    pub kinds: &'a [&'a str],
    pub limit: usize,
    /// a coarse position (rounded by the route), for ranking only
    pub near: Option<(f64, f64)>,
}

pub struct Found {
    pub intent: Value,
    /// (score, the result as stored, internal fields still in)
    pub results: Vec<(f64, Value)>,
}

// the scale
const CODE: f64 = 1000.0;
const INTENT: f64 = 900.0;
const INTENT_STEP: f64 = 40.0;
const WHOLE: f64 = 600.0;
const WHOLE_NAME: f64 = 40.0;
const PREFIX: f64 = 500.0;
const TYPO1: f64 = 470.0;
const TYPO2: f64 = 380.0;
const SOME: f64 = 150.0;
const SECONDARY: f64 = 200.0;
/// results under this take turns by kind (three of a kind, then others)
const MIXED_BELOW: f64 = 800.0;

fn lift(pop: u64, weight: f64) -> f64 {
    weight * (1.0 + pop as f64).log10()
}

impl Engine {
    pub fn open(dir: &Path) -> anyhow::Result<Engine> {
        let index = Index::open(super::readonly::ReadOnly::open(dir)?)?;
        let f = fields(&index.schema())?;
        let reader = index.reader_builder().reload_policy(ReloadPolicy::Manual).try_into()?;
        let lex: Lexicon = serde_json::from_slice(&std::fs::read(dir.join("lexicon.json"))?)?;
        Ok(Engine { generation: lex.generation.clone(), as_of: lex.as_of.clone(), reader, f, lx: Lookup::new(lex) })
    }

    pub fn search(&self, ask: &Ask) -> tantivy::Result<Found> {
        let s = Search { e: self, searcher: self.reader.searcher(), ask, found: HashMap::new() };
        s.run()
    }
}

struct Cand {
    score: f64,
    doc: Value,
}

struct Search<'a> {
    e: &'a Engine,
    searcher: Searcher,
    ask: &'a Ask<'a>,
    /// (kind, id) -> best candidate
    found: HashMap<(String, String), Cand>,
}

fn text_term(f: tantivy::schema::Field, s: &str) -> Term {
    Term::from_field_text(f, s)
}

impl Search<'_> {
    fn f(&self) -> Fields {
        self.e.f
    }

    /// `q` restricted to `kinds` (and to what the caller asked for).
    fn only(&self, q: Box<dyn Query>, kinds: &[&str]) -> Box<dyn Query> {
        let allowed: Vec<&str> = kinds.iter().copied().filter(|k| self.ask.kinds.contains(k)).collect();
        let kind: Box<dyn Query> = Box::new(TermSetQuery::new(allowed.iter().map(|k| text_term(self.f().kind, k))));
        Box::new(BooleanQuery::new(vec![(Occur::Must, q), (Occur::Must, kind)]))
    }

    fn any_of(&self, field: tantivy::schema::Field, values: &[String]) -> Box<dyn Query> {
        Box::new(TermSetQuery::new(values.iter().map(|v| text_term(field, v))))
    }

    /// Top `n` by popularity (structured lookups: every match is as good).
    fn by_pop(&self, q: &dyn Query, n: usize) -> tantivy::Result<Vec<(u64, Value)>> {
        let top = TopDocs::with_limit(n).tweak_score(|seg: &tantivy::SegmentReader| {
            let pop = seg.fast_fields().u64("pop").ok().map(|c| c.first_or_default_col(0));
            move |doc: tantivy::DocId, _score: tantivy::Score| pop.as_ref().map_or(0, |c| c.get_val(doc))
        });
        let hits = self.searcher.search(q, &top)?;
        hits.into_iter().map(|(pop, addr)| Ok((pop, self.load(addr)?))).collect()
    }

    /// Top `n` by BM25 lifted a little by popularity: (bm25, pop, doc).
    fn by_relevance(&self, q: &dyn Query, n: usize) -> tantivy::Result<Vec<(f32, u64, Value)>> {
        let top = TopDocs::with_limit(n).tweak_score(|seg: &tantivy::SegmentReader| {
            let pop = seg.fast_fields().u64("pop").ok().map(|c| c.first_or_default_col(0));
            move |doc: tantivy::DocId, score: tantivy::Score| {
                let p = pop.as_ref().map_or(0, |c| c.get_val(doc));
                (score as f64 + 0.6 * (1.0 + p as f64).ln(), score, p)
            }
        });
        let hits = self.searcher.search(q, &top)?;
        hits.into_iter().map(|((_, bm25, pop), addr)| Ok((bm25, pop, self.load(addr)?))).collect()
    }

    fn load(&self, addr: DocAddress) -> tantivy::Result<Value> {
        let d: TantivyDocument = self.searcher.doc(addr)?;
        let bytes = d.get_first(self.f().doc).and_then(|v| v.as_bytes()).unwrap_or(b"null");
        Ok(serde_json::from_slice(bytes).unwrap_or(Value::Null))
    }

    fn offer(&mut self, score: f64, doc: Value) {
        let key = (doc["kind"].as_str().unwrap_or("").to_string(), doc["id"].as_str().unwrap_or("").to_string());
        let score = score + self.signals(&doc);
        match self.found.get_mut(&key) {
            Some(c) if c.score >= score => {}
            Some(c) => c.score = score,
            None => {
                self.found.insert(key, Cand { score, doc });
            }
        }
    }

    /// Signals beside relevance. Now: nearness to the caller's coarse
    /// position, for airports (up to 15 within 300 km). Later: flights
    /// airborne now, recency.
    fn signals(&self, doc: &Value) -> f64 {
        let (Some((lat, lon)), Some(ll)) = (self.ask.near, doc["_ll"].as_array()) else { return 0.0 };
        let (Some(a), Some(b)) = (ll.first().and_then(Value::as_f64), ll.get(1).and_then(Value::as_f64)) else { return 0.0 };
        let km = haversine_km(lat, lon, a, b);
        15.0 * (1.0 - km / 300.0).max(0.0)
    }

    fn run(mut self) -> tantivy::Result<Found> {
        let q = self.ask.q;
        let intents = parse(q, &self.e.lx);
        let mut reading: Option<Value> = None;
        let mut about = None;
        for (r, it) in intents.iter().enumerate() {
            let base = INTENT - INTENT_STEP * r as f64;
            let n = self.intent(it, r, base)?;
            if n > 0 && reading.is_none() {
                reading = Some(intent_json(it));
                about = Some(agrees_with(it));
            }
        }
        let codes = self.codes(about)?;
        if reading.is_none() && codes > 0 {
            reading = Some(json!({"kind": "code", "code": compact(q)}));
        }
        self.text()?;
        let mut all: Vec<Cand> = self.found.into_values().collect();
        all.sort_by(|a, b| {
            b.score
                .total_cmp(&a.score)
                .then_with(|| kind_order(&a.doc).cmp(&kind_order(&b.doc)))
                .then_with(|| a.doc["id"].as_str().cmp(&b.doc["id"].as_str()))
        });
        // below the readings' results, kinds take turns: three of one,
        // then the others' best
        let mut out: Vec<Cand> = vec![];
        let mut later: Vec<Cand> = vec![];
        let mut per: HashMap<String, usize> = HashMap::new();
        for c in all {
            let k = c.doc["kind"].as_str().unwrap_or("").to_string();
            let n = per.entry(k).or_default();
            if c.score >= MIXED_BELOW || *n < 3 {
                *n += 1;
                out.push(c);
            } else {
                later.push(c);
            }
        }
        out.extend(later);
        out.truncate(self.ask.limit);
        Ok(Found {
            intent: reading.unwrap_or_else(|| json!({"kind": "text"})),
            results: out.into_iter().map(|c| (c.score, c.doc)).collect(),
        })
    }

    /// The results of one reading; how many.
    fn intent(&mut self, it: &Intent, rank: usize, base: f64) -> tantivy::Result<usize> {
        let f = self.f();
        let mut n = 0;
        match it {
            Intent::Flight { callsigns, .. } => {
                // the number itself: as good as a code when it is the
                // likeliest reading
                let exact = if rank == 0 { CODE + 30.0 } else { base };
                let q = self.only(self.any_of(f.codes, callsigns), &["flight"]);
                for (pop, doc) in self.by_pop(&*q, 10)? {
                    self.offer(exact + lift(pop, 2.0), doc);
                    n += 1;
                }
                // numbers that start with it, busiest first
                let prefixes: Vec<(Occur, Box<dyn Query>)> = callsigns
                    .iter()
                    .map(|c| (Occur::Should, Box::new(FuzzyTermQuery::new_prefix(text_term(f.codes, c), 0, true)) as Box<dyn Query>))
                    .collect();
                let q = self.only(Box::new(BooleanQuery::new(prefixes)), &["flight"]);
                for (pop, doc) in self.by_pop(&*q, 10)? {
                    self.offer(PREFIX + lift(pop, 12.0), doc);
                    n += 1;
                }
            }
            Intent::Registration { bare } => {
                let exact = if rank == 0 { CODE + 30.0 } else { base };
                let q = self.only(Box::new(TermQuery::new(text_term(f.codes, bare), IndexRecordOption::Basic)), &["aircraft"]);
                for (_, doc) in self.by_pop(&*q, 3)? {
                    self.offer(exact, doc);
                    n += 1;
                }
            }
            Intent::Route { from, to } => {
                let mut pairs = vec![];
                for a in &from.airports {
                    for b in &to.airports {
                        pairs.push(format!("{a}>{b}"));
                    }
                }
                let q = self.only(self.any_of(f.pairs, &pairs), &["flight"]);
                for (pop, doc) in self.by_pop(&*q, 40)? {
                    // a nonstop first, the flight that is just this route
                    // before one passing through; how often that leg flies
                    let fit = route_fit(&doc, from, to);
                    let s = base + if fit.direct { 20.0 } else { 0.0 } + if fit.whole { 10.0 } else { 0.0 } - 3.0 * fit.at as f64
                        + lift(fit.flights.unwrap_or(pop), 6.0);
                    self.offer(s, doc);
                    n += 1;
                }
            }
            Intent::Fleet { airline, types, .. } => {
                let q: Box<dyn Query> = Box::new(BooleanQuery::new(vec![
                    (Occur::Must, Box::new(TermQuery::new(text_term(f.airline, airline), IndexRecordOption::Basic)) as Box<dyn Query>),
                    (Occur::Must, self.any_of(f.types, types)),
                ]));
                let q = self.only(q, &["aircraft"]);
                for (_, doc) in self.by_pop(&*q, 20)? {
                    let at = types.iter().position(|t| doc["type"].as_str() == Some(t)).unwrap_or(types.len());
                    self.offer(base + 10.0 - at.min(10) as f64, doc);
                    n += 1;
                }
                // the type itself, beneath its fleet
                let q = self.only(self.any_of(f.codes, types), &["type"]);
                for (pop, doc) in self.by_pop(&*q, 3)? {
                    self.offer(base - 100.0 + lift(pop, 6.0), doc);
                }
            }
            Intent::AirlinePlace { airline, place } => {
                let q: Box<dyn Query> = Box::new(BooleanQuery::new(vec![
                    (Occur::Must, Box::new(TermQuery::new(text_term(f.airline, airline), IndexRecordOption::Basic)) as Box<dyn Query>),
                    (Occur::Must, self.any_of(f.airports, &place.airports)),
                ]));
                let q = self.only(q, &["flight"]);
                for (pop, doc) in self.by_pop(&*q, 40)? {
                    // flights that begin or end there first, the main
                    // airport first, by how often the legs there fly
                    let fit = place_fit(&doc, place);
                    let s = base + if fit.end { 10.0 } else { 0.0 } - 3.0 * fit.at as f64 + lift(fit.flights.unwrap_or(pop), 6.0);
                    self.offer(s, doc);
                    n += 1;
                }
            }
            Intent::Type { types, .. } => {
                let q = self.only(self.any_of(f.codes, types), &["type"]);
                for (pop, doc) in self.by_pop(&*q, 20)? {
                    let at = types.iter().position(|t| doc["id"].as_str() == Some(t)).unwrap_or(types.len());
                    self.offer(base + 10.0 - at.min(10) as f64 + lift(pop, 1.0), doc);
                    n += 1;
                }
            }
            Intent::City { place } => {
                let q = self.only(self.any_of(f.codes, &place.airports), &["airport"]);
                for (pop, doc) in self.by_pop(&*q, 20)? {
                    let at = place.airports.iter().position(|a| doc["id"].as_str() == Some(a)).unwrap_or(place.airports.len());
                    self.offer(base + 20.0 - 3.0 * at.min(6) as f64 + lift(pop, 1.0), doc);
                    n += 1;
                }
                // and the busiest flights from its main airports, beneath
                let main: Vec<String> = place.airports.iter().take(2).cloned().collect();
                let q = self.only(self.any_of(f.airports, &main), &["flight"]);
                for (pop, doc) in self.by_pop(&*q, 3)? {
                    self.offer(SECONDARY + lift(pop, 6.0), doc);
                }
            }
        }
        Ok(n)
    }

    /// The query as a code, typed whole (no spaces): any kind whose code
    /// it is.
    ///
    /// When the query has a reading, a code of another kind than the
    /// reading's ranks beneath the readings ("A350" is the family before
    /// Aegean's flight A3 50; "787" before a tail marked 787).
    fn codes(&mut self, reading: Option<&str>) -> tantivy::Result<usize> {
        let q = self.ask.q;
        if q.contains(' ') {
            return Ok(0);
        }
        let beneath = INTENT - INTENT_STEP * 3.0;
        let mut terms = vec![q.to_string(), compact(q)];
        terms.dedup();
        let has_digit = q.chars().any(|c| c.is_ascii_digit());
        let letters_only = compact(q).chars().all(|c| c.is_ascii_alphabetic());
        // a word that is a place or an airline is not a tail ("DUBAI"
        // is not D-UBAI): such a registration ranks as a guess
        let ws = words(q);
        let is_word = letters_only && (self.e.lx.place(&ws).is_some() || self.e.lx.airline_named(&ws).is_some());
        let query = self.only(self.any_of(self.f().codes, &terms), KINDS);
        let mut n = 0;
        for (pop, doc) in self.by_pop(&*query, 20)? {
            let kind = doc["kind"].as_str().unwrap_or("");
            // a callsign without a number ("CDG") is noise
            if kind == "flight" && !doc["id"].as_str().unwrap_or("").chars().any(|c| c.is_ascii_digit()) {
                continue;
            }
            let bonus = match (kind, has_digit, compact(q).len()) {
                ("flight", true, _) => 30.0,
                ("aircraft", true, _) => 25.0,
                ("type", true, _) => 20.0,
                ("airline", false, 2) => 30.0,
                ("airport", false, 3) => 30.0,
                ("airline", false, 3) => 25.0,
                ("airport", false, 4) => 30.0,
                ("type", false, 4) => 25.0,
                ("aircraft", false, _) => 20.0,
                _ => 10.0,
            };
            let s = if kind == "aircraft" && letters_only && is_word {
                TYPO1 - 20.0
            } else if reading.is_some_and(|r| r != kind) {
                beneath + bonus / 10.0 + lift(pop, 2.0)
            } else {
                CODE + bonus + lift(pop, 2.0)
            };
            self.offer(s, doc);
            n += 1;
        }
        // registrations that start with it ("9V-SM"), in registration order
        if has_digit && compact(q).len() >= 3 && self.ask.kinds.contains(&"aircraft") {
            let terms: Vec<Box<dyn Query>> = terms
                .iter()
                .map(|t| Box::new(FuzzyTermQuery::new_prefix(text_term(self.f().codes, t), 0, true)) as Box<dyn Query>)
                .collect();
            let query = self.only(Box::new(BooleanQuery::union(terms)), &["aircraft"]);
            let mut frames = self.by_pop(&*query, 50)?;
            frames.sort_by(|a, b| a.1["reg"].as_str().cmp(&b.1["reg"].as_str()));
            for (i, (_, doc)) in frames.into_iter().enumerate() {
                self.offer(PREFIX + 20.0 - 0.1 * i as f64, doc);
            }
        }
        Ok(n)
    }

    /// Names: airports (with their cities), airlines, types. Words whole,
    /// the last one perhaps still being typed; near misses only when
    /// nothing matched whole.
    fn text(&mut self) -> tantivy::Result<()> {
        let qw = words(self.ask.q);
        if qw.is_empty() || !qw.iter().any(|w| w.chars().any(char::is_alphabetic)) {
            return Ok(());
        }
        let whole = self.text_pass(&qw, false)?;
        let strong = self.found.values().any(|c| c.score >= INTENT - INTENT_STEP * 2.0);
        if !whole && !strong {
            self.text_pass(&qw, true)?;
        }
        Ok(())
    }

    /// One pass; whether something matched every word whole.
    fn text_pass(&mut self, qw: &[String], typos: bool) -> tantivy::Result<bool> {
        let f = self.f();
        let last = qw.len() - 1;
        let mut per_word: Vec<(Occur, Box<dyn Query>)> = vec![];
        for (i, w) in qw.iter().enumerate() {
            let mut any: Vec<(Occur, Box<dyn Query>)> =
                vec![(Occur::Should, Box::new(TermQuery::new(text_term(f.text, w), IndexRecordOption::WithFreqs)))];
            if i == last && (w.chars().count() >= 2 || qw.len() > 1) {
                any.push((Occur::Should, Box::new(FuzzyTermQuery::new_prefix(text_term(f.text, w), 0, true))));
            }
            let edits = allowed_edits(w);
            if typos && edits > 0 {
                any.push((Occur::Should, Box::new(FuzzyTermQuery::new(text_term(f.text, w), edits, true))));
            }
            per_word.push((Occur::Should, Box::new(BooleanQuery::new(any))));
        }
        let q = self.only(Box::new(BooleanQuery::new(per_word)), &["airport", "airline", "type"]);
        let hits = self.by_relevance(&*q, 60)?;
        let top = hits.iter().map(|h| h.0).fold(0.0f32, f32::max).max(1e-6);
        let qphrase = qw.join(" ");
        let mut whole = false;
        for (bm25, pop, doc) in hits {
            let m: Vec<String> = doc["_m"].as_array().map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect()).unwrap_or_default();
            let Some(fit) = fit(qw, &m, typos) else { continue };
            let rel = 3.0 * (bm25 / top) as f64;
            let s = match fit {
                Fit::Whole => {
                    whole = true;
                    WHOLE + if m.iter().any(|x| *x == qphrase) { WHOLE_NAME } else { 0.0 } + lift(pop, 6.0)
                }
                Fit::Prefix => PREFIX + lift(pop, 12.0),
                Fit::Typo(1) => TYPO1 + lift(pop, 12.0),
                Fit::Typo(_) => TYPO2 + lift(pop, 12.0),
                Fit::Some(frac) => SOME + 150.0 * frac + lift(pop, 6.0),
            };
            self.offer(s + rel, doc);
        }
        Ok(whole)
    }
}

#[derive(Debug, PartialEq)]
enum Fit {
    Whole,
    Prefix,
    Typo(usize),
    Some(f64),
}

/// How the query's words meet a document's names: every word whole,
/// the last as a prefix, some within their allowed edits, or only some
/// words at all.
fn fit(qw: &[String], names: &[String], typos: bool) -> Option<Fit> {
    let dw: Vec<&str> = names.iter().flat_map(|n| n.split(' ')).collect();
    let last = qw.len() - 1;
    let (mut matched, mut prefix, mut worst) = (0usize, false, 0usize);
    for (i, w) in qw.iter().enumerate() {
        if dw.iter().any(|d| d == w) {
            matched += 1;
            continue;
        }
        if i == last && dw.iter().any(|d| d.starts_with(w.as_str())) {
            matched += 1;
            prefix = true;
            continue;
        }
        let cap = allowed_edits(w) as usize;
        if typos && cap > 0 {
            if let Some(d) = dw.iter().map(|d| distance(w, d, cap)).filter(|&d| d <= cap).min() {
                matched += 1;
                worst = worst.max(d);
            }
        }
    }
    if matched == 0 {
        return None;
    }
    Some(if matched < qw.len() {
        Fit::Some(matched as f64 / qw.len() as f64)
    } else if worst > 0 {
        Fit::Typo(worst)
    } else if prefix {
        Fit::Prefix
    } else {
        Fit::Whole
    })
}

/// How a flight fits a route: whether it flies it as one leg (and how
/// often, that leg), whether the route is the whole flight, and how far
/// down the two places' airport lists its ends are.
struct RouteFit {
    direct: bool,
    whole: bool,
    at: usize,
    flights: Option<u64>,
}

fn route_fit(doc: &Value, from: &Place, to: &Place) -> RouteFit {
    let mut fit = RouteFit { direct: false, whole: false, at: 0, flights: None };
    let route: Vec<&str> = doc["route"].as_array().map(|r| r.iter().filter_map(Value::as_str).collect()).unwrap_or_default();
    for leg in doc["legs"].as_array().into_iter().flatten() {
        if from.airports.iter().any(|a| leg["org"].as_str() == Some(a)) && to.airports.iter().any(|b| leg["dst"].as_str() == Some(b)) {
            fit.direct = true;
            fit.flights = fit.flights.max(leg["flights"].as_u64());
        }
    }
    if let (Some(first), Some(last)) = (route.first(), route.last()) {
        fit.whole = from.airports.iter().any(|a| a == first) && to.airports.iter().any(|b| b == last);
    }
    let mut best = usize::MAX;
    for (i, a) in route.iter().enumerate() {
        let Some(pa) = from.airports.iter().position(|x| x == a) else { continue };
        for b in &route[i + 1..] {
            if let Some(pb) = to.airports.iter().position(|x| x == b) {
                best = best.min(pa + pb);
            }
        }
    }
    if best != usize::MAX {
        fit.at = best;
    }
    fit
}

/// How a flight fits a place: whether it begins or ends there, how far
/// down the place's airport list, how often its legs there fly.
struct PlaceFit {
    end: bool,
    at: usize,
    flights: Option<u64>,
}

fn place_fit(doc: &Value, place: &Place) -> PlaceFit {
    let route: Vec<&str> = doc["route"].as_array().map(|r| r.iter().filter_map(Value::as_str).collect()).unwrap_or_default();
    let pos = |code: &str| place.airports.iter().position(|a| a == code);
    let end = [route.first(), route.last()].into_iter().flatten().any(|c| pos(c).is_some());
    let at = route.iter().filter_map(|c| pos(c)).min().unwrap_or(0);
    let flights = doc["legs"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|l| [l["org"].as_str(), l["dst"].as_str()].into_iter().flatten().any(|c| pos(c).is_some()))
        .filter_map(|l| l["flights"].as_u64())
        .max();
    PlaceFit { end, at, flights }
}

/// The kind of result a reading is about.
fn agrees_with(it: &Intent) -> &'static str {
    match it {
        Intent::Flight { .. } | Intent::Route { .. } | Intent::AirlinePlace { .. } => "flight",
        Intent::Registration { .. } | Intent::Fleet { .. } => "aircraft",
        Intent::Type { .. } => "type",
        Intent::City { .. } => "airport",
    }
}

fn kind_order(doc: &Value) -> u8 {
    match doc["kind"].as_str() {
        Some("flight") => 0,
        Some("aircraft") => 1,
        Some("airport") => 2,
        Some("airline") => 3,
        _ => 4,
    }
}

fn place_json(p: &Place) -> Value {
    json!({"text": p.text, "airports": p.airports, "how": p.how})
}

fn intent_json(it: &Intent) -> Value {
    match it {
        Intent::Flight { number, callsigns } => json!({"kind": it.kind(), "number": number, "callsigns": callsigns}),
        Intent::Registration { bare } => json!({"kind": it.kind(), "registration": bare}),
        Intent::Route { from, to } => json!({"kind": it.kind(), "from": place_json(from), "to": place_json(to)}),
        Intent::Fleet { airline, family, types } => json!({"kind": it.kind(), "airline": airline, "family": family, "types": types}),
        Intent::AirlinePlace { airline, place } => json!({"kind": it.kind(), "airline": airline, "place": place_json(place)}),
        Intent::Type { family, types } => json!({"kind": it.kind(), "family": family, "types": types}),
        Intent::City { place } => json!({"kind": it.kind(), "place": place_json(place)}),
    }
}

pub fn haversine_km(a_lat: f64, a_lon: f64, b_lat: f64, b_lon: f64) -> f64 {
    let (p1, p2) = (a_lat.to_radians(), b_lat.to_radians());
    let dp = (b_lat - a_lat).to_radians();
    let dl = (b_lon - a_lon).to_radians();
    let h = (dp / 2.0).sin().powi(2) + p1.cos() * p2.cos() * (dl / 2.0).sin().powi(2);
    2.0 * 6371.0 * h.sqrt().asin()
}

/// A stored result as served: internal fields out, the score in, the
/// site's address made absolute.
pub fn served(score: f64, mut doc: Value, site: &str) -> Value {
    let Some(m) = doc.as_object_mut() else { return doc };
    let mut out = Map::with_capacity(m.len() + 1);
    for (k, v) in std::mem::take(m) {
        if k.starts_with('_') {
            continue;
        }
        if k == "url" {
            let url = v.as_str().map(|rel| format!("{}/{}", site.trim_end_matches('/'), rel));
            out.insert(k, url.map_or(Value::Null, Value::String));
            continue;
        }
        out.insert(k, v);
    }
    out.insert("score".into(), json!((score * 10.0).round() / 10.0));
    Value::Object(out)
}

#[cfg(test)]
mod tests {
    use super::super::text::phrase;
    use super::*;

    fn w(s: &str) -> Vec<String> {
        words(s)
    }

    fn names(s: &[&str]) -> Vec<String> {
        s.iter().map(|x| phrase(x)).collect()
    }

    #[test]
    fn fits_whole_prefix_typo_some() {
        let changi = names(&["Singapore Changi Airport", "Singapore"]);
        assert_eq!(fit(&w("changi"), &changi, false), Some(Fit::Whole));
        assert_eq!(fit(&w("changi airport"), &changi, false), Some(Fit::Whole));
        assert_eq!(fit(&w("changi air"), &changi, false), Some(Fit::Prefix));
        assert_eq!(fit(&w("chnagi"), &changi, false), None);
        assert_eq!(fit(&w("chnagi"), &changi, true), Some(Fit::Typo(1)));
        assert_eq!(fit(&w("changi london"), &changi, false), Some(Fit::Some(0.5)));
        let qantas = names(&["Qantas"]);
        assert_eq!(fit(&w("qantsa"), &qantas, true), Some(Fit::Typo(1)));
        // four letters take one edit: not Scott for "scoo"… but Scoot for "scot"
        assert_eq!(fit(&w("scot"), &names(&["Scoot"]), true), Some(Fit::Typo(1)));
        assert_eq!(fit(&w("sco"), &names(&["Scoot"]), true), Some(Fit::Prefix));
    }

    #[test]
    fn typos_never_reach_a_whole_match() {
        // the best a near miss can be lifted to stays under a whole
        // match with no traffic at all, and a prefix with none
        let most = lift(10_000_000, 12.0);
        assert!(TYPO1 + most + 3.0 < WHOLE);
        assert!(PREFIX + most + 3.0 < WHOLE);
        assert!(WHOLE + WHOLE_NAME + lift(10_000_000, 6.0) + 3.0 + 15.0 < INTENT - INTENT_STEP * 2.0);
        // a city's busiest flights stay beneath any name being typed
        assert!(SECONDARY + lift(10_000_000, 6.0) < PREFIX);
    }

    #[test]
    fn a_route_leg_before_a_stretch() {
        let doc = json!({"route": ["SYD", "SIN", "LHR"], "legs": [{"org": "SYD", "dst": "SIN", "flights": 30}, {"org": "SIN", "dst": "LHR", "flights": 31}]});
        let p = |a: &[&str]| Place { text: String::new(), airports: a.iter().map(|s| s.to_string()).collect(), how: "code" };
        let f = |a: &[&str], b: &[&str]| {
            let r = route_fit(&doc, &p(a), &p(b));
            (r.direct, r.whole, r.at, r.flights)
        };
        assert_eq!(f(&["SIN"], &["LHR"]), (true, false, 0, Some(31)));
        assert_eq!(f(&["SYD"], &["LHR"]), (false, true, 0, None));
        assert_eq!(f(&["JFK", "SYD"], &["LGW", "LHR"]), (false, true, 2, None));
    }

    #[test]
    fn a_place_at_either_end_before_one_on_the_way() {
        let p = Place { text: String::new(), airports: vec!["LHR".into(), "LGW".into()], how: "city" };
        let doc = json!({"route": ["SIN", "LHR"], "legs": [{"org": "SIN", "dst": "LHR", "flights": 26}]});
        let f = place_fit(&doc, &p);
        assert_eq!((f.end, f.at, f.flights), (true, 0, Some(26)));
        let doc = json!({"route": ["CPH", "LGW", "AER"], "legs": [
            {"org": "CPH", "dst": "LGW", "flights": 40}, {"org": "LGW", "dst": "AER", "flights": 46}]});
        let f = place_fit(&doc, &p);
        assert_eq!((f.end, f.at, f.flights), (false, 1, Some(46)));
    }

    #[test]
    fn served_drops_internals_and_makes_urls_absolute() {
        let doc = json!({"kind": "airport", "id": "SIN", "url": "?airport=SIN", "_m": ["x"], "_ll": [1.0, 2.0]});
        let v = served(1030.04, doc, "https://flightportrait.com/network");
        assert_eq!(v, json!({"kind": "airport", "id": "SIN", "url": "https://flightportrait.com/network/?airport=SIN", "score": 1030.0}));
        let v = served(800.0, json!({"kind": "type", "id": "A388", "url": null}), "https://x/");
        assert_eq!(v["url"], Value::Null);
        assert!(haversine_km(1.35, 103.99, 51.47, -0.45) > 10_800.0);
    }
}
