//! How /v2 reads text, at index time and at query time alike: accents
//! off (/v1's folding table, then any combining mark left by a
//! decomposed spelling, then the few letters that fold to two), case
//! off, split into words on anything that is not a letter or a digit.
//! "São Paulo", "SAO PAULO" and "Sa\u{0303}o Paulo" are the same two
//! words; "Winston-Salem" is two words.

/// Accent- and case-folded, lowercase.
pub fn fold(s: &str) -> String {
    let once = crate::search::fold(s);
    let mut out = String::with_capacity(once.len());
    for c in once.chars() {
        match c {
            '\u{0300}'..='\u{036f}' => {}
            'ß' | 'ẞ' => out.push_str("ss"),
            'Æ' | 'æ' => out.push_str("ae"),
            'Œ' | 'œ' => out.push_str("oe"),
            'Þ' | 'þ' => out.push_str("th"),
            c => out.extend(c.to_lowercase()),
        }
    }
    out
}

/// The folded words of `s`.
pub fn words(s: &str) -> Vec<String> {
    fold(s).split(|c: char| !c.is_alphanumeric()).filter(|w| !w.is_empty()).map(str::to_string).collect()
}

/// The folded words of `s`, one space between them: how a name is
/// compared whole.
pub fn phrase(s: &str) -> String {
    words(s).join(" ")
}

/// A code as the index keeps it: uppercase, letters and digits only
/// ("9V-SMA" and "9v sma" -> "9VSMA").
pub fn compact(s: &str) -> String {
    s.chars().filter(|c| c.is_ascii_alphanumeric()).map(|c| c.to_ascii_uppercase()).collect()
}

/// Edits a typed word may be from what it means: none under four
/// letters, one for four or five, two from six.
pub fn allowed_edits(word: &str) -> u8 {
    match word.chars().count() {
        0..=3 => 0,
        4..=5 => 1,
        _ => 2,
    }
}

/// Optimal string alignment distance (a swap of neighbours is one
/// edit), over characters, stopping early past `cap`.
pub fn distance(a: &str, b: &str, cap: usize) -> usize {
    let a: Vec<char> = a.chars().collect();
    let b: Vec<char> = b.chars().collect();
    if a.len().abs_diff(b.len()) > cap {
        return cap + 1;
    }
    let w = b.len() + 1;
    let mut d = vec![0usize; (a.len() + 1) * w];
    for i in 0..=a.len() {
        d[i * w] = i;
    }
    for (j, cell) in d.iter_mut().enumerate().take(w) {
        *cell = j;
    }
    for i in 1..=a.len() {
        let mut row_min = usize::MAX;
        for j in 1..=b.len() {
            let cost = usize::from(a[i - 1] != b[j - 1]);
            let mut v = (d[(i - 1) * w + j] + 1).min(d[i * w + j - 1] + 1).min(d[(i - 1) * w + j - 1] + cost);
            if i > 1 && j > 1 && a[i - 1] == b[j - 2] && a[i - 2] == b[j - 1] {
                v = v.min(d[(i - 2) * w + j - 2] + 1);
            }
            d[i * w + j] = v;
            row_min = row_min.min(v);
        }
        if row_min > cap {
            return cap + 1;
        }
    }
    d[a.len() * w + b.len()]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn folds_accents_case_and_decomposed_spellings() {
        assert_eq!(fold("São Paulo"), "sao paulo");
        assert_eq!(fold("SA\u{0303}O PAULO"), "sao paulo");
        assert_eq!(fold("Malmo\u{0308} Aviation"), "malmo aviation");
        assert_eq!(fold("München"), "munchen");
        assert_eq!(fold("Straße"), "strasse");
        assert_eq!(fold("Widerøe"), "wideroe");
        assert_eq!(fold("Łódź"), "lodz");
        assert_eq!(fold("İstanbul"), "istanbul");
        assert_eq!(words("Winston-Salem"), ["winston", "salem"]);
        assert_eq!(words("Köln (Cologne)"), ["koln", "cologne"]);
        assert_eq!(phrase("  Air   Caraïbes "), "air caraibes");
        assert_eq!(compact("9v-sma"), "9VSMA");
        assert_eq!(compact("SQ 322"), "SQ322");
    }

    #[test]
    fn edits_by_length_and_swaps_count_once() {
        assert_eq!((allowed_edits("SIN"), allowed_edits("SCOT"), allowed_edits("ETIHD"), allowed_edits("QANTSA")), (0, 1, 1, 2));
        assert_eq!(distance("qantsa", "qantas", 2), 1);
        assert_eq!(distance("frankfrut", "frankfurt", 2), 1);
        assert_eq!(distance("chnagi", "changi", 2), 1);
        assert_eq!(distance("ryanar", "ryanair", 2), 1);
        assert_eq!(distance("scot", "scoot", 1), 1);
        assert_eq!(distance("qantsa", "santa", 2), 2);
        assert_eq!(distance("qantsa", "sanya", 2), 3);
        assert_eq!(distance("abc", "abc", 0), 0);
    }
}
