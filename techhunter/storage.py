"""Backward-compatible facade over the repository layer.

The single DB implementation lives in db/repository.py (RepositoryContainer);
this module re-exports the same methods as plain functions for the Telegram
handlers and tests. Feedback business logic lives in feedback.py.
"""
from .db import get_session
from .db.repository import RepositoryContainer
from .feedback import (  # noqa: F401  (re-exported for handlers/tests)
    evaluate_personal_penalty,
    feedback_features,
    feedback_personal_penalty,
    feedback_threshold_multiplier,
)

_repos = RepositoryContainer(get_session)

# ─── Users ───────────────────────────────────────────────────────────────────
upsert_user = _repos.users.upsert_user
set_approved = _repos.users.set_approved
is_user_approved = _repos.users.is_user_approved
discovery_users = _repos.users.discovery_users
all_user_ids = _repos.users.all_user_ids
set_paused = _repos.users.set_paused
is_paused = _repos.users.is_paused
get_delivery_prefs = _repos.users.get_delivery_prefs
set_min_score = _repos.users.set_min_score
set_exclude_shop = _repos.users.set_exclude_shop
toggle_exclude_condition = _repos.users.toggle_exclude_condition
set_discovery_enabled = _repos.users.set_discovery_enabled
set_discovery_profit = _repos.users.set_discovery_profit
set_discovery_scan_mode = _repos.users.set_discovery_scan_mode

# ─── Subscriptions ───────────────────────────────────────────────────────────
add_subscription = _repos.subscriptions.add_subscription
active_subscriptions = _repos.subscriptions.active_subscriptions
list_subscriptions = _repos.subscriptions.list_subscriptions
get_subscription = _repos.subscriptions.get_subscription
remove_subscription = _repos.subscriptions.remove_subscription
toggle_subscription_paused = _repos.subscriptions.toggle_subscription_paused
mark_subscription_onboarded = _repos.subscriptions.mark_subscription_onboarded

# ─── Listings (dedup / evaluation cache) ─────────────────────────────────────
get_cached_listing = _repos.listings.get_cached_listing
check_content_duplicate = _repos.listings.check_content_duplicate
cache_listing = _repos.listings.cache_listing
cache_listing_skipped = _repos.listings.cache_listing_skipped

# ─── Alerts / deal cards / outbox ────────────────────────────────────────────
save_card_state = _repos.alerts.save_card_state
get_card_state = _repos.alerts.get_card_state
alert_already_sent = _repos.alerts.alert_already_sent
content_alert_already_sent = _repos.alerts.content_alert_already_sent
mark_alert_sent = _repos.alerts.mark_alert_sent
queue_pending_alert = _repos.alerts.queue_pending_alert
list_pending_alerts = _repos.alerts.list_pending_alerts
mark_pending_alert_attempt = _repos.alerts.mark_pending_failed
mark_pending_sent = _repos.alerts.mark_pending_sent
clear_dead_pending_alerts = _repos.alerts.clear_dead_pending_alerts
pending_alert_stats = _repos.alerts.pending_alert_stats

# ─── Image hashes (photo-reuse scam signal) ──────────────────────────────────
count_reused_images = _repos.images.count_reused_images
record_image_hash = _repos.images.record_image_hash

# ─── Maintenance ─────────────────────────────────────────────────────────────
cleanup_old_rows = _repos.maintenance.cleanup_old_rows
dev_db_stats = _repos.maintenance.dev_db_stats

# ─── Feedback ────────────────────────────────────────────────────────────────
record_feedback = _repos.feedback.record_feedback
feedback_stats = _repos.feedback.feedback_stats
feedback_reason_stats = _repos.feedback.feedback_reason_stats

__all__ = [
    "get_session",
    "upsert_user", "set_approved", "is_user_approved", "discovery_users",
    "all_user_ids", "set_paused", "is_paused", "get_delivery_prefs",
    "set_min_score", "set_exclude_shop", "toggle_exclude_condition",
    "set_discovery_enabled", "set_discovery_profit", "set_discovery_scan_mode",
    "add_subscription", "active_subscriptions", "list_subscriptions",
    "get_subscription", "remove_subscription", "toggle_subscription_paused",
    "mark_subscription_onboarded",
    "get_cached_listing", "check_content_duplicate", "cache_listing",
    "cache_listing_skipped",
    "save_card_state", "get_card_state", "alert_already_sent",
    "content_alert_already_sent",
    "mark_alert_sent", "queue_pending_alert", "list_pending_alerts",
    "mark_pending_alert_attempt", "mark_pending_sent",
    "clear_dead_pending_alerts", "pending_alert_stats",
    "count_reused_images", "record_image_hash",
    "cleanup_old_rows", "dev_db_stats",
    "record_feedback", "feedback_stats", "feedback_reason_stats",
    "evaluate_personal_penalty", "feedback_features",
    "feedback_personal_penalty", "feedback_threshold_multiplier",
]
