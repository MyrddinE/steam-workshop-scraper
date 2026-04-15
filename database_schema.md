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
| `creator` | TEXT | The Steam ID of the item's creator. |
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

**Primary Key:** `workshop_id`

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
