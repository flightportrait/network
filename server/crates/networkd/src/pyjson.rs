//! JSON written the way the Python service writes it, so responses stay
//! byte-compatible: compact separators, UTF-8 left unescaped, and floats
//! in Python's `repr` form (`1.0`, `1e-05`, `1e+16`).
//!
//! Values read from readsb are kept as [`Val`]: integers stay integers
//! and decimals become floats, exactly as `json.loads` would leave them.

use serde_json::Value;

#[derive(Clone, Debug, PartialEq)]
pub enum Val {
    Null,
    Bool(bool),
    Int(i64),
    UInt(u64),
    Float(f64),
    Str(Box<str>),
    /// Arrays and objects, already encoded.
    Raw(Box<str>),
}

impl Val {
    pub fn from_json(v: &Value) -> Val {
        match v {
            Value::Null => Val::Null,
            Value::Bool(b) => Val::Bool(*b),
            Value::Number(n) => {
                if let Some(i) = n.as_i64() {
                    Val::Int(i)
                } else if let Some(u) = n.as_u64() {
                    Val::UInt(u)
                } else {
                    Val::Float(n.as_f64().unwrap_or(0.0))
                }
            }
            Value::String(s) => Val::Str(s.as_str().into()),
            other => {
                let mut out = String::new();
                write_value(&mut out, other);
                Val::Raw(out.into())
            }
        }
    }

    /// Parse one raw JSON token (a field value from a readsb line).
    pub fn from_raw(raw: &str) -> Val {
        match raw.as_bytes().first() {
            Some(b'"') => match serde_json::from_str::<String>(raw) {
                Ok(s) => Val::Str(s.into()),
                Err(_) => Val::Null,
            },
            Some(b'n') => Val::Null,
            Some(b't') => Val::Bool(true),
            Some(b'f') => Val::Bool(false),
            _ => match serde_json::from_str::<Value>(raw) {
                Ok(v) => Val::from_json(&v),
                Err(_) => Val::Null,
            },
        }
    }

    pub fn as_f64(&self) -> Option<f64> {
        match *self {
            Val::Int(i) => Some(i as f64),
            Val::UInt(u) => Some(u as f64),
            Val::Float(f) => Some(f),
            Val::Bool(b) => Some(b as i64 as f64),
            _ => None,
        }
    }

    pub fn is_null(&self) -> bool {
        matches!(self, Val::Null)
    }

    pub fn write(&self, out: &mut String) {
        match self {
            Val::Null => out.push_str("null"),
            Val::Bool(true) => out.push_str("true"),
            Val::Bool(false) => out.push_str("false"),
            Val::Int(i) => {
                use std::fmt::Write;
                let _ = write!(out, "{i}");
            }
            Val::UInt(u) => {
                use std::fmt::Write;
                let _ = write!(out, "{u}");
            }
            Val::Float(f) => write_float(out, *f),
            Val::Str(s) => write_str(out, s),
            Val::Raw(r) => out.push_str(r),
        }
    }
}

/// Python's `json.dumps` of a float (`float.__repr__`). Non-finite
/// values cannot occur in what we serve (the Python service would fail
/// on them too); they are written as `null` rather than invalid JSON.
pub fn write_float(out: &mut String, f: f64) {
    if !f.is_finite() {
        out.push_str("null");
        return;
    }
    if f == 0.0 {
        out.push_str(if f.is_sign_negative() { "-0.0" } else { "0.0" });
        return;
    }
    // shortest round-trip digits from ryu, which, like CPython's dtoa,
    // breaks an exact tie between two shortest candidates to the even
    // digit (-70.785797119140625 -> ...062, where std's formatter says
    // ...063). ryu writes "100.0", "0.00001", "1e16", "1.25e-7".
    let mut buf = ryu::Buffer::new();
    let text = buf.format_finite(f);
    let (neg, text) = match text.strip_prefix('-') {
        Some(t) => (true, t),
        None => (false, text),
    };
    let (mant, e) = match text.split_once('e') {
        Some((m, e)) => (m, e.parse::<i32>().unwrap()),
        None => (text, 0),
    };
    let (int_part, frac) = mant.split_once('.').unwrap_or((mant, ""));
    let all: String = [int_part, frac].concat();
    let lead = all.bytes().take_while(|b| *b == b'0').count();
    let trimmed = all[lead..].trim_end_matches('0');
    let digits = if trimmed.is_empty() { "0".to_string() } else { trimmed.to_string() };
    let exp: i32 = int_part.len() as i32 - 1 - lead as i32 + e;
    if neg {
        out.push('-');
    }
    if (-4..16).contains(&exp) {
        if exp < 0 {
            out.push_str("0.");
            for _ in 0..(-exp - 1) {
                out.push('0');
            }
            out.push_str(&digits);
        } else {
            let int_len = exp as usize + 1;
            if digits.len() <= int_len {
                out.push_str(&digits);
                for _ in 0..(int_len - digits.len()) {
                    out.push('0');
                }
                out.push_str(".0");
            } else {
                out.push_str(&digits[..int_len]);
                out.push('.');
                out.push_str(&digits[int_len..]);
            }
        }
    } else {
        out.push_str(&digits[..1]);
        if digits.len() > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        out.push('e');
        out.push(if exp < 0 { '-' } else { '+' });
        let a = exp.unsigned_abs();
        if a < 10 {
            out.push('0');
        }
        use std::fmt::Write;
        let _ = write!(out, "{a}");
    }
}

/// Python's `round(x, n)` for the small `n` we use: the double nearest
/// to the correctly rounded decimal (ties to even on the exact value).
pub fn round_to(x: f64, n: usize) -> f64 {
    if !x.is_finite() {
        return x;
    }
    format!("{x:.n$}").parse().unwrap_or(x)
}

/// `json.dumps(s, ensure_ascii=False)`.
pub fn write_str(out: &mut String, s: &str) {
    out.push('"');
    let mut start = 0;
    for (i, c) in s.char_indices() {
        let esc: Option<&str> = match c {
            '"' => Some("\\\""),
            '\\' => Some("\\\\"),
            '\n' => Some("\\n"),
            '\r' => Some("\\r"),
            '\t' => Some("\\t"),
            '\u{8}' => Some("\\b"),
            '\u{c}' => Some("\\f"),
            c if (c as u32) < 0x20 => None,
            _ => continue,
        };
        out.push_str(&s[start..i]);
        match esc {
            Some(e) => out.push_str(e),
            None => {
                use std::fmt::Write;
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
        }
        start = i + c.len_utf8();
    }
    out.push_str(&s[start..]);
    out.push('"');
}

pub fn write_value(out: &mut String, v: &Value) {
    match v {
        Value::Array(a) => {
            out.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_value(out, x);
            }
            out.push(']');
        }
        Value::Object(o) => {
            out.push('{');
            for (i, (k, x)) in o.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_str(out, k);
                out.push(':');
                write_value(out, x);
            }
            out.push('}');
        }
        other => Val::from_json(other).write(out),
    }
}

/// Small builder for the compact objects the routes return.
pub struct Obj<'a> {
    out: &'a mut String,
    first: bool,
}

impl<'a> Obj<'a> {
    pub fn new(out: &'a mut String) -> Obj<'a> {
        out.push('{');
        Obj { out, first: true }
    }

    pub fn key(&mut self, k: &str) -> &mut String {
        if !self.first {
            self.out.push(',');
        }
        self.first = false;
        write_str(self.out, k);
        self.out.push(':');
        self.out
    }

    pub fn f64(&mut self, k: &str, v: f64) -> &mut Self {
        write_float(self.key(k), v);
        self
    }

    pub fn int(&mut self, k: &str, v: i64) -> &mut Self {
        use std::fmt::Write;
        let _ = write!(self.key(k), "{v}");
        self
    }

    pub fn str(&mut self, k: &str, v: &str) -> &mut Self {
        write_str(self.key(k), v);
        self
    }

    pub fn null(&mut self, k: &str) -> &mut Self {
        self.key(k).push_str("null");
        self
    }

    pub fn raw(&mut self, k: &str, v: &str) -> &mut Self {
        self.key(k).push_str(v);
        self
    }

    pub fn end(self) {
        self.out.push('}');
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn f(x: f64) -> String {
        let mut s = String::new();
        write_float(&mut s, x);
        s
    }

    #[test]
    fn floats_like_python_repr() {
        // expectations from CPython 3.12 repr()
        let cases = [
            (1.0, "1.0"), (0.1, "0.1"), (-2.5, "-2.5"), (100.0, "100.0"),
            (1.3, "1.3"), (1e-05, "1e-05"), (1.5e-05, "1.5e-05"),
            (0.0001, "0.0001"), (1e16, "1e+16"), (1.2345e16, "1.2345e+16"),
            (123456789012345678.0, "1.2345678901234568e+17"),
            (9999999999999998.0, "9999999999999998.0"),
            (1790257773.510103, "1790257773.510103"), (103.8198, "103.8198"),
            (-0.0, "-0.0"), (0.0, "0.0"), (1e22, "1e+22"), (5e-324, "5e-324"),
            (451.3, "451.3"), (2.0e-4, "0.0002"),
            // exact ties between two shortest strings go to the even digit
            (-70.78579711914062, "-70.78579711914062"), // exactly -70.785797119140625
            (0.3, "0.3"), (1.0 / 3.0, "0.3333333333333333"), (2.5, "2.5"),
            (1234567.0, "1234567.0"), (1e15, "1000000000000000.0"),
            (123e-20, "1.23e-18"), (-1.5e300, "-1.5e+300"),
        ];
        for (x, want) in cases {
            assert_eq!(f(x), want, "{x:e}");
        }
    }

    #[test]
    fn rounding_like_python() {
        // round(x, 1) in CPython
        assert_eq!(round_to(0.25, 1), 0.2);
        assert_eq!(round_to(0.35, 1), 0.3); // 0.35 is below .35 in binary
        assert_eq!(round_to(2.675, 2), 2.67);
        assert_eq!(round_to(1790257773.46, 1), 1790257773.5);
        assert_eq!(round_to(12.05, 1), 12.1); // 12.05 is above in binary
    }

    #[test]
    fn strings_like_python() {
        let mut s = String::new();
        write_str(&mut s, "a\"b\\c\nd\u{1}é/");
        assert_eq!(s, "\"a\\\"b\\\\c\\nd\\u0001é/\"");
    }

    #[test]
    fn raw_tokens_parse_like_json_loads() {
        let mut s = String::new();
        for raw in ["1.300000", "25", "\"SIA1  \"", "null", "-1.5e3", "[1, 2.50]", "true"] {
            Val::from_raw(raw).write(&mut s);
            s.push(' ');
        }
        assert_eq!(s, "1.3 25 \"SIA1  \" null -1500.0 [1,2.5] true ");
    }
}

