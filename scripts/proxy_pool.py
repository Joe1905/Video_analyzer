#!/usr/bin/env python3
"""Explicit compatibility exports for web and legacy task callers.

Remove exports as those active callers migrate to the proxy package.
"""

from __future__ import annotations

from proxy.state import (
    _row_to_session,
    _active_sessions,
    cleanup_expired_sessions,
    list_state,
)

from proxy.pools import (
    upsert_pool,
    get_pool,
    delete_pool,
    require_account_proxy_bound,
    schedule_proxy_recheck_for_pending_job,
    check_binding,
    recheck_unavailable_proxies,
    _direct_login_pool_id,
)

from proxy.accounts import (
    bootstrap_instagram_profile,
    account_avatar_bytes,
    upsert_account,
    get_account,
    sync_feishu_directory,
    delete_account,
    delete_account_platform,
    update_account_proxy_binding,
    start_login_session,
    stop_login_session,
    open_observation_platform,
    start_automation_session,
    claim_observation_session_for_job,
    release_observation_session_job,
    finish_automation_session,
    handoff_automation_session,
    inspect_login_session,
    capture_pending_login_sessions,
    update_account_status,
)

from proxy.products import (
    list_products,
    create_product,
    update_product,
    delete_product,
)

from proxy.repository import (
    connect,
)
from proxy.nodes import (
    parse_static_proxy_uri,
    parse_vless_uri,
    parse_vmess_uri,
)
from proxy.runtime import (
    _hidden_automation_slot,
    _mihomo_listener_matches,
    ensure_proxy_cores,
    ensure_static_proxy_configs,
    mihomo_export,
    reconcile_mihomo_pool_configs,
    runtime_status,
)
from proxy.settings import (
    ACCOUNT_STATUS_ACTIVE,
    ACCOUNT_STATUS_PAUSED,
    PORT_SCOPE_DEFAULT,
    PROXY_QUEUE_RECHECK_SECONDS,
    RUNTIME_ID,
    STATUS_ACTIVE,
    STATUS_DUPLICATE,
    STATUS_ERROR,
    STATUS_PAUSED,
    SYSTEM_PROXY_DIALER,
    browser_max_slots,
    hidden_automation_slots,
    is_retryable_proxy_error,
    now_iso,
    visible_observation_slots,
)
