//! In-memory image config cache.
//!
//! Stores image metadata (CMD, ENTRYPOINT, ENV, WORKDIR, `diff_ids`)
//! after the first Docker Engine API call so subsequent lookups are
//! zero-network, zero-subprocess.
//!
//! Analogous to Podman's `containers/storage` `images.json`, but
//! much simpler — just a `HashMap` behind a `Mutex`.

use std::collections::HashMap;
use std::sync::{LazyLock, Mutex};

use serde::{Deserialize, Serialize};

/// Cached image configuration.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ImageConfig {
    pub image_id: String,
    pub diff_ids: Vec<String>,
    pub cmd: Option<Vec<String>>,
    pub entrypoint: Option<Vec<String>>,
    pub env: HashMap<String, String>,
    pub working_dir: Option<String>,
    pub exposed_ports: Vec<u16>,
}

/// Global in-memory store: image name → config.
static STORE: LazyLock<Mutex<HashMap<String, ImageConfig>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

/// Look up cached config by image name.
pub fn get(image_name: &str) -> Option<ImageConfig> {
    STORE.lock().unwrap().get(image_name).cloned()
}

/// Store config under one or more names.
pub fn put(image_name: &str, config: ImageConfig) {
    let mut store = STORE.lock().unwrap();
    // Also index by image_id so lookups by digest work.
    let id = config.image_id.clone();
    store.insert(image_name.to_owned(), config.clone());
    if !id.is_empty() && id != image_name {
        store.insert(id, config);
    }
}

/// Clear all cached entries.
pub fn clear() {
    STORE.lock().unwrap().clear();
}
