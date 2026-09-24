//! The only module that talks to the upstream readsb (or, in point
//! mode, a public aggregator).

use std::time::Duration;

use bytes::Bytes;

pub struct Upstream {
    base: String,
    client: reqwest::Client,
}

#[derive(Debug)]
pub struct Status(pub u16);

impl std::fmt::Display for Status {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        write!(f, "upstream HTTP {}", self.0)
    }
}

impl std::error::Error for Status {}

impl Upstream {
    pub fn new(base: &str, timeout_s: f64) -> Upstream {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs_f64(timeout_s))
            .user_agent("fp-network-api")
            .build()
            .expect("http client");
        Upstream { base: base.trim_end_matches('/').to_string(), client }
    }

    pub async fn get(&self, path: &str) -> anyhow::Result<Bytes> {
        self.get_url(&format!("{}{}", self.base, path)).await
    }

    pub async fn get_url(&self, url: &str) -> anyhow::Result<Bytes> {
        let r = self.client.get(url).send().await?;
        if !r.status().is_success() {
            return Err(Status(r.status().as_u16()).into());
        }
        Ok(r.bytes().await?)
    }

    /// The day's trace for one aircraft (tar1090's trace_full).
    pub async fn trace(&self, hex: &str) -> anyhow::Result<Bytes> {
        let h = hex.trim().to_lowercase();
        let tail = h.get(4..6).unwrap_or("");
        self.get(&format!("/data/traces/{tail}/trace_full_{h}.json")).await
    }
}

pub fn status_of(e: &anyhow::Error) -> Option<u16> {
    e.downcast_ref::<Status>().map(|s| s.0)
}
