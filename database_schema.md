# Database Schema

This document outlines the schema for the SQLite database used by the Steam Workshop Scraper.

## `workshop_items` table

This table stores the metadata for each Steam Workshop item.

| Column Name | Data Type | Description |
|---|---|---|
| `workshop_id` | INTEGER | The unique ID of the workshop item. This is the primary key. |
| `dt_found` | TEXT | ISO 8601 timestamp of when the item was first discovered by the scraper. |
| `dt_updated` | TEXT | ISO 8601 timestamp of the last successful metadata fetch. |
| `dt_attempted` | TEXT | ISO 8601 timestamp of the last attempt to fetch metadata. |
| `status` | INTEGER | The status code of the last fetch attempt (e.g., 200 for success, 404 for not found). |
| `title` | TEXT | The title of the workshop item. |
| `creator` | INTEGER | The Steam ID of the item's creator. |
| `creator_appid` | INTEGER | The App ID of the tool used to create the item. |
| `consumer_appid` | INTEGER | The App ID of the game this item is for. |
| `filename` | TEXT | The name of the primary file. |
| `file_size` | INTEGER | The size of the primary file in bytes. |
| `preview_url` | TEXT | The URL for the preview image. |
| `hcontent_file` | TEXT | The handle for the primary file content. |
| `hcontent_preview` | TEXT | The handle for the preview image content. |
| `short_description` | TEXT | The short description of the item. |
| `time_created` | INTEGER | Unix timestamp of when the item was created. |
| `time_updated` | INTEGER | Unix timestamp of when the item was last updated. |
| `visibility` | INTEGER | The visibility state of the item (e.g., 0 for Public). |
| `banned` | INTEGER | Boolean (0 or 1) indicating if the item is banned. |
| `ban_reason` | TEXT | The reason for the ban. |
| `app_name` | TEXT | The name of the associated game. |
| `file_type` | INTEGER | The type of file (e.g., 0 for a community-made item). |
| `subscriptions` | INTEGER | The number of current subscribers. |
| `favorited` | INTEGER | The number of times the item has been favorited. |
| `views` | INTEGER | The number of times the item has been viewed. |
| `tags` | TEXT | A JSON array of tags associated with the item. |
| `extended_description` | TEXT | The full, scraped description from the workshop page. |
| `language` | INTEGER | The detected language ID. |
| `lifetime_subscriptions` | INTEGER | Total lifetime subscriptions. |
| `lifetime_favorited` | INTEGER | Total lifetime favorites. |
| `title_en` | TEXT | Translated English title. |
| `short_description_en` | TEXT | Translated English short description. |
| `extended_description_en` | TEXT | Translated English extended description. |
| `dt_translated` | TEXT | ISO 8601 timestamp of translation. |
| `translation_priority` | INTEGER | Priority for translation queue. |
| `is_queued_for_subscription` | INTEGER | Boolean (0 or 1) for TUI subscription queue. |

**Primary Key:** `workshop_id`

## `users` table

Stores metadata for Steam users (creators).

| Column Name | Data Type | Description |
|---|---|---|
| `steamid` | INTEGER | The unique Steam ID 64. Primary Key. |
| `personaname` | TEXT | The user's persona name. |
| `personaname_en` | TEXT | Translated English persona name. |
| `dt_updated` | TEXT | ISO 8601 timestamp of last update. |
| `dt_translated` | TEXT | ISO 8601 timestamp of translation. |
| `translation_priority` | INTEGER | Priority for translation queue. |

## `app_tracking` table

Tracks scraping progress and filters per AppID.

| Column Name | Data Type | Description |
|---|---|---|
| `appid` | INTEGER | The Steam AppID. Primary Key. |
| `last_historical_date_scanned` | INTEGER | Unix timestamp of the last scanned item for historical scraping. |
| `filter_text` | TEXT | Text filter for scraping. |
| `required_tags` | TEXT | JSON array of required tags for scraping. |
| `excluded_tags` | TEXT | JSON array of excluded tags for scraping. |
| `window_size` | INTEGER | The time window (in seconds) for scraping. |

## Indexes

To ensure efficient querying, the following indexes are created:

| Index Name | Column(s) | Purpose |
|---|---|---|
| `idx_consumer_appid` | `consumer_appid` | Filtering by game. |
| `idx_status` | `status` | Filtering for items that need to be retried. |
| `idx_dt_updated` | `dt_updated` | Filtering by last update time. |
| `idx_dt_attempted` | `dt_attempted` | Filtering by last attempt time. |
| `idx_title` | `title` | Searching by title. |
| `idx_tags` | `tags` | Searching by tags. |
| `idx_creator` | `creator` | Filtering by item creator. |
| `idx_short_description` | `short_description` | Searching within the short description. |
| `idx_extended_description` | `extended_description` | Searching within the extended description. |
