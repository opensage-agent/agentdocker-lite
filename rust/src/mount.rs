//! Overlay mount helpers.
//!
//! 1. **New mount API** (kernel >= 6.8): rustix `fsopen` + `fsconfig_set_string`
//!    with `lowerdir+` per-layer append. No length limit per layer.
//! 2. **Legacy mount(2)** fallback via nix: single syscall, `PAGE_SIZE` limit.

use std::io;
use std::sync::OnceLock;

// --- overlay parameter detection (matching buildkit overlayutils) ---

static NEW_API_SUPPORTED: OnceLock<bool> = OnceLock::new();
static OVERLAY_INDEX_OFF: OnceLock<bool> = OnceLock::new();
static OVERLAY_REDIRECT_DIR_OFF: OnceLock<bool> = OnceLock::new();

/// Check if `userxattr` is needed and supported (matching buildkit's
/// `NeedsUserXAttr`).  Kernel >= 5.11 in a user namespace always
/// needs it; older kernels with backports may or may not support it.
pub fn needs_userxattr() -> bool {
    *OVERLAY_USERXATTR.get_or_init(|| {
        // Parse kernel version from /proc/version
        let Ok(ver) = std::fs::read_to_string("/proc/version") else {
            return false;
        };
        // "Linux version 6.8.0-87-generic ..."
        let (major, minor) = parse_kernel_version(&ver);
        if major > 5 || (major == 5 && minor >= 11) {
            return true;
        }
        // Pre-5.11: conservative — don't use userxattr
        log::debug!("Kernel {major}.{minor} < 5.11, skipping userxattr");
        false
    })
}

static OVERLAY_USERXATTR: OnceLock<bool> = OnceLock::new();

fn parse_kernel_version(ver: &str) -> (u32, u32) {
    // "Linux version 6.8.0-87-generic ..." → (6, 8)
    for word in ver.split_whitespace() {
        if let Some(dot) = word.find('.') {
            if let Ok(major) = word[..dot].parse::<u32>() {
                let rest = &word[dot + 1..];
                let minor_end = rest.find(|c: char| !c.is_ascii_digit()).unwrap_or(rest.len());
                let minor = rest[..minor_end].parse::<u32>().unwrap_or(0);
                return (major, minor);
            }
        }
    }
    (0, 0)
}

/// Check if the overlay module supports the `index` parameter.
fn overlay_supports_index() -> bool {
    *OVERLAY_INDEX_OFF.get_or_init(|| {
        std::fs::read_to_string("/sys/module/overlay/parameters/index")
            .map(|s| !s.trim().is_empty())
            .unwrap_or(false)
    })
}

/// Check if the overlay module supports `redirect_dir` (and it's enabled).
/// If enabled, we should explicitly set `redirect_dir=off` to avoid rename
/// issues (matching buildkit's `setRedirectDir` logic).
fn overlay_redirect_dir_needs_off() -> bool {
    *OVERLAY_REDIRECT_DIR_OFF.get_or_init(|| {
        std::fs::read_to_string("/sys/module/overlay/parameters/redirect_dir")
            .map(|s| {
                let v = s.trim();
                // "Y" or "y" or "on" means enabled → we need to disable it
                v.eq_ignore_ascii_case("y") || v.eq_ignore_ascii_case("on")
            })
            .unwrap_or(false)
    })
}

pub fn check_new_mount_api() -> bool {
    *NEW_API_SUPPORTED.get_or_init(|| {
        let Ok(fd) = rustix::mount::fsopen("overlay", rustix::mount::FsOpenFlags::FSOPEN_CLOEXEC)
        else {
            return false;
        };

        // Try lowerdir+ — if kernel < 6.8, this will EINVAL
        let supported = rustix::mount::fsconfig_set_string(&fd, "lowerdir+", "/").is_ok();

        log::debug!("New mount API (lowerdir+): {supported}");
        supported
    })
}

// --- new mount API via rustix ---

fn mount_overlay_new_api(
    lower_dirs: &[&str],
    upper_dir: &str,
    work_dir: &str,
    target: &str,
    extra_opts: &[&str],
) -> io::Result<()> {
    let fd = rustix::mount::fsopen("overlay", rustix::mount::FsOpenFlags::FSOPEN_CLOEXEC)?;

    // Add each lower layer individually (lowerdir+ appends top-to-bottom)
    for layer in lower_dirs {
        rustix::mount::fsconfig_set_string(&fd, "lowerdir+", *layer)?;
    }

    rustix::mount::fsconfig_set_string(&fd, "upperdir", upper_dir)?;
    rustix::mount::fsconfig_set_string(&fd, "workdir", work_dir)?;

    // Extra options (e.g. "userxattr" for rootless)
    for opt in extra_opts {
        if let Some((key, val)) = opt.split_once('=') {
            rustix::mount::fsconfig_set_string(&fd, key, val)?;
        } else {
            // Boolean flag — use fsconfig_set_flag
            rustix::mount::fsconfig_set_flag(&fd, *opt)?;
        }
    }

    rustix::mount::fsconfig_create(&fd)?;

    let mnt = rustix::mount::fsmount(
        &fd,
        rustix::mount::FsMountFlags::FSMOUNT_CLOEXEC,
        rustix::mount::MountAttrFlags::empty(),
    )?;

    rustix::mount::move_mount(
        &mnt,
        "",
        rustix::fs::CWD,
        target,
        rustix::mount::MoveMountFlags::MOVE_MOUNT_F_EMPTY_PATH,
    )?;

    Ok(())
}

// --- legacy mount(2) via nix ---

fn mount_overlay_legacy(
    lowerdir_spec: &str,
    upper_dir: &str,
    work_dir: &str,
    target: &str,
    extra_opts: &[&str],
) -> io::Result<()> {
    let mut options = format!("lowerdir={lowerdir_spec},upperdir={upper_dir},workdir={work_dir}");
    for opt in extra_opts {
        options.push(',');
        options.push_str(opt);
    }

    nix::mount::mount(
        Some("overlay"),
        target,
        Some("overlay"),
        nix::mount::MsFlags::empty(),
        Some(options.as_str()),
    )
    .map_err(|e| io::Error::from_raw_os_error(e as i32))
}

// --- public API ---

/// Mount overlayfs, auto-selecting the best available method.
///
/// `extra_opts`: additional mount options (e.g. `&["userxattr"]` for rootless).
/// Passed as individual `fsconfig` flags in the new API, or comma-joined in
/// the legacy `mount(2)` data string.
///
/// Automatically adds `index=off` and `redirect_dir=off` when the kernel
/// overlay module supports them (matching buildkit's overlay snapshotter).
pub fn mount_overlay(
    lowerdir_spec: &str,
    upper_dir: &str,
    work_dir: &str,
    target: &str,
    extra_opts: &[&str],
) -> io::Result<()> {
    let lower_dirs: Vec<&str> = lowerdir_spec.split(':').collect();

    // Build effective options: caller's extra_opts + auto-detected overlay params
    let mut opts: Vec<&str> = extra_opts.to_vec();
    let has_userxattr = opts.iter().any(|o| *o == "userxattr");

    if overlay_supports_index() {
        opts.push("index=off");
    }
    // redirect_dir conflicts with userxattr in userns (matching buildkit)
    if !has_userxattr && overlay_redirect_dir_needs_off() {
        opts.push("redirect_dir=off");
    }

    if check_new_mount_api() {
        match mount_overlay_new_api(&lower_dirs, upper_dir, work_dir, target, &opts) {
            Ok(()) => return Ok(()),
            Err(e) => {
                log::warn!("New mount API failed, falling back to legacy mount(2): {e}");
            }
        }
    }

    mount_overlay_legacy(lowerdir_spec, upper_dir, work_dir, target, &opts)
}

/// Bind mount `source` onto `target`.
pub fn bind_mount(source: &str, target: &str) -> io::Result<()> {
    nix::mount::mount(
        Some(source),
        target,
        None::<&str>,
        nix::mount::MsFlags::MS_BIND,
        None::<&str>,
    )
    .map_err(|e| io::Error::from_raw_os_error(e as i32))
}

/// Recursive bind mount (`mount --rbind`).
pub fn rbind_mount(source: &str, target: &str) -> io::Result<()> {
    nix::mount::mount(
        Some(source),
        target,
        None::<&str>,
        nix::mount::MsFlags::MS_BIND | nix::mount::MsFlags::MS_REC,
        None::<&str>,
    )
    .map_err(|e| io::Error::from_raw_os_error(e as i32))
}

/// Make a mount point private (`mount --make-private`).
pub fn make_private(target: &str) -> io::Result<()> {
    nix::mount::mount(
        None::<&str>,
        target,
        None::<&str>,
        nix::mount::MsFlags::MS_PRIVATE,
        None::<&str>,
    )
    .map_err(|e| io::Error::from_raw_os_error(e as i32))
}

/// Remount a bind mount as read-only (`mount -o remount,ro,bind`).
pub fn remount_ro_bind(target: &str) -> io::Result<()> {
    nix::mount::mount(
        None::<&str>,
        target,
        None::<&str>,
        nix::mount::MsFlags::MS_REMOUNT
            | nix::mount::MsFlags::MS_RDONLY
            | nix::mount::MsFlags::MS_BIND,
        None::<&str>,
    )
    .map_err(|e| io::Error::from_raw_os_error(e as i32))
}

/// Lazy unmount (`umount -l`).
pub fn umount_lazy(target: &str) -> io::Result<()> {
    nix::mount::umount2(target, nix::mount::MntFlags::MNT_DETACH)
        .map_err(|e| io::Error::from_raw_os_error(e as i32))
}

/// Regular unmount.
pub fn umount(target: &str) -> io::Result<()> {
    nix::mount::umount2(target, nix::mount::MntFlags::empty())
        .map_err(|e| io::Error::from_raw_os_error(e as i32))
}

/// Recursive lazy unmount (`umount -R -l`).
///
/// First tries recursive unmount via `MNT_DETACH`.  The kernel doesn't
/// have a single "recursive + detach" flag, so we scan `/proc/self/mountinfo`
/// and lazily unmount every sub-mount bottom-up before the target itself.
pub fn umount_recursive_lazy(target: &str) -> io::Result<()> {
    // Read mountinfo to find all sub-mounts under `target`.
    let minfo = std::fs::read_to_string("/proc/self/mountinfo")?;
    let mut sub_mounts: Vec<String> = Vec::new();

    for line in minfo.lines() {
        // Fields: id parent_id major:minor root mount_point ...
        let fields: Vec<&str> = line.split_whitespace().collect();
        if fields.len() >= 5 {
            let mount_point = fields[4];
            if mount_point.starts_with(target) {
                sub_mounts.push(mount_point.to_string());
            }
        }
    }

    // Sort by length descending (deepest first).
    sub_mounts.sort_by_key(|m| std::cmp::Reverse(m.len()));

    for mp in &sub_mounts {
        let _ = nix::mount::umount2(mp.as_str(), nix::mount::MntFlags::MNT_DETACH);
    }

    Ok(())
}
