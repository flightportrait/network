//! The index's one schema. One index holds every kind of document (a
//! `kind` field tells them apart): one reader, one reload, one
//! generation, and a query that spans kinds is one search, ranked in
//! one pass. The documents are small and few (about a million), so the
//! shared term statistics cost nothing that matters here.
//!
//! Fields:
//! - `kind`: flight, airline, airport, aircraft or type
//! - `codes`: every code the thing answers to, uppercase (callsign and
//!   marketed number; ICAO and IATA; ident; hex and registration, with
//!   and without its dash; designator)
//! - `text`: the folded words of its names (airport, city and its other
//!   names; airline; type and family). Flights and airframes carry no
//!   text: they are found by code or through an intent.
//! - `pairs`: a flight's ordered airport pairs ("SIN>LHR"), every leg
//!   and every stretch of a multi-leg flight
//! - `airports`: every airport a flight touches
//! - `airline`: a flight's airline, an airframe's operator (ICAO)
//! - `types`: a flight's types, an airframe's type
//! - `pop`: how often it flies or is flown from (flights, departures,
//!   airframes): the popularity prior
//! - `doc`: the result as served (JSON), with the words it matches on

use tantivy::schema::{Field, IndexRecordOption, Schema, TextFieldIndexing, TextOptions, FAST, STORED};

#[derive(Clone, Copy)]
pub struct Fields {
    pub kind: Field,
    pub codes: Field,
    pub text: Field,
    pub pairs: Field,
    pub airports: Field,
    pub airline: Field,
    pub types: Field,
    pub pop: Field,
    pub doc: Field,
}

pub fn schema() -> (Schema, Fields) {
    let mut b = Schema::builder();
    let text = TextOptions::default()
        .set_indexing_options(TextFieldIndexing::default().set_tokenizer("default").set_index_option(IndexRecordOption::WithFreqs));
    // codes and filters: whole values, no lengths kept (nothing ranks
    // by them)
    let raw = TextOptions::default().set_indexing_options(
        TextFieldIndexing::default().set_tokenizer("raw").set_index_option(IndexRecordOption::Basic).set_fieldnorms(false),
    );
    let f = Fields {
        kind: b.add_text_field("kind", raw.clone()),
        codes: b.add_text_field("codes", raw.clone()),
        text: b.add_text_field("text", text),
        pairs: b.add_text_field("pairs", raw.clone()),
        airports: b.add_text_field("airports", raw.clone()),
        airline: b.add_text_field("airline", raw.clone()),
        types: b.add_text_field("types", raw),
        pop: b.add_u64_field("pop", FAST),
        doc: b.add_bytes_field("doc", STORED),
    };
    (b.build(), f)
}

pub fn fields(s: &Schema) -> tantivy::Result<Fields> {
    Ok(Fields {
        kind: s.get_field("kind")?,
        codes: s.get_field("codes")?,
        text: s.get_field("text")?,
        pairs: s.get_field("pairs")?,
        airports: s.get_field("airports")?,
        airline: s.get_field("airline")?,
        types: s.get_field("types")?,
        pop: s.get_field("pop")?,
        doc: s.get_field("doc")?,
    })
}

pub const KINDS: &[&str] = &["flight", "airline", "airport", "aircraft", "type"];
