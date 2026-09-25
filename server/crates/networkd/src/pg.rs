//! Postgres, for the writers that keep the shared record (the database
//! the nightly also maintains). Same NETWORK_API_DATABASE_URL as the
//! Python service; its SQLAlchemy driver suffix is dropped.

pub fn url(sqlalchemy_url: &str) -> String {
    match sqlalchemy_url.split_once("://") {
        Some((scheme, rest)) => {
            let scheme = scheme.split('+').next().unwrap_or(scheme);
            format!("{scheme}://{rest}")
        }
        None => sqlalchemy_url.to_string(),
    }
}

pub async fn connect(database_url: &str) -> Result<tokio_postgres::Client, tokio_postgres::Error> {
    let (client, connection) = tokio_postgres::connect(&url(database_url), tokio_postgres::NoTls).await?;
    tokio::spawn(async move {
        if let Err(e) = connection.await {
            eprintln!("postgres: {e}");
        }
    });
    Ok(client)
}

/// A connection opened on first use and reopened after a loss; one
/// caller at a time.
pub struct Lazy {
    url: String,
    client: tokio::sync::Mutex<Option<tokio_postgres::Client>>,
}

impl Lazy {
    pub fn new(url: &str) -> Lazy {
        Lazy { url: url.to_string(), client: tokio::sync::Mutex::new(None) }
    }

    pub async fn get(
        &self,
    ) -> Result<tokio::sync::MutexGuard<'_, Option<tokio_postgres::Client>>, tokio_postgres::Error> {
        let mut g = self.client.lock().await;
        if g.as_ref().is_none_or(|c| c.is_closed()) {
            *g = Some(connect(&self.url).await?);
        }
        Ok(g)
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn drops_the_driver() {
        assert_eq!(super::url("postgresql+psycopg://u:p@db:5432/netapi"), "postgresql://u:p@db:5432/netapi");
        assert_eq!(super::url("postgresql://u@h/d"), "postgresql://u@h/d");
    }
}
