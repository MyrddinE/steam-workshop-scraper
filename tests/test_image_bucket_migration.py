import os
import shutil
import sqlite3
import pytest
from src.database import (
    initialize_database,
    get_connection,
    get_image_subdirs,
    get_image_path
)
from src.webserver import app, init_webserver

def test_get_image_subdirs_and_path():
    # Test valid integers and strings
    assert get_image_subdirs(1039919954) == ('2', '5', 'b')
    assert get_image_subdirs("1039919954") == ('2', '5', 'b')
    
    # Test path construction
    assert get_image_path("images", 1039919954, "jpg") == os.path.join("images", "2", "5", "b", "1039919954.jpg")
    
    # Test invalid values fallback gracefully
    assert get_image_subdirs("invalid_id") == ("0", "0", "0")
    assert get_image_subdirs(None) == ("0", "0", "0")

def test_migration_12_to_13(tmp_path):
    # Setup paths
    db_path = str(tmp_path / "test_migration.db")
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    
    # 1. Initialize database to schema version 13 first to get the full schema
    initialize_database(db_path)
    
    # 2. Roll back the schema user_version to 12
    conn = get_connection(db_path)
    conn.execute("PRAGMA user_version = 12")
    conn.commit()
    
    # 3. Insert dummy test data into the fully formed tables
    cursor = conn.cursor()
    cursor.execute("INSERT INTO workshop_items (workshop_id, image_extension, needs_image) VALUES (1039919954, 'jpg', 0)")
    cursor.execute("INSERT INTO workshop_items (workshop_id, image_extension, needs_image) VALUES (1055818964, 'png', 0)")
    cursor.execute("INSERT INTO workshop_items (workshop_id, image_extension, needs_image) VALUES (123456789, 'gif', 0)")
    conn.commit()
    conn.close()
    
    # Create dummy images in the filesystem
    # Flat image for Case A
    flat_img_a = images_dir / "1039919954.jpg"
    flat_img_a.write_text("dummy_image_data_a")
    
    # Already migrated image for Case B
    char1, char2, char3 = get_image_subdirs(1055818964)
    nested_dir_b = images_dir / char1 / char2 / char3
    nested_dir_b.mkdir(parents=True, exist_ok=True)
    nested_img_b = nested_dir_b / "1055818964.png"
    nested_img_b.write_text("dummy_image_data_b")
    
    # 4. Run initialize_database again (triggers migration 12->13 because db_version = 12)
    initialize_database(db_path)
    
    # 5. Assert database version bumped to the terminal version (12->13 image
    # buckets, 13->14 renames, 14->15 full-text index, 15->16 stranded-row recovery)
    conn = get_connection(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version == 16
    
    # 6. Assert files were migrated correctly
    # Case A should be moved
    nested_path_a = images_dir / "2" / "5" / "b" / "1039919954.jpg"
    assert nested_path_a.exists()
    assert nested_path_a.read_text() == "dummy_image_data_a"
    assert not flat_img_a.exists()
    
    # Case B should remain at nested path and not be disturbed
    assert nested_img_b.exists()
    assert nested_img_b.read_text() == "dummy_image_data_b"

def test_serve_image_resolution(tmp_path):
    # Setup database and images dir
    db_path = str(tmp_path / "test_web.db")
    initialize_database(db_path)
    
    images_dir = tmp_path / "images"
    images_dir.mkdir(exist_ok=True)
    
    # Let's write a dummy image file at the nested path
    wid = 1039919954
    char1, char2, char3 = get_image_subdirs(wid)
    nested_dir = images_dir / char1 / char2 / char3
    nested_dir.mkdir(parents=True, exist_ok=True)
    img_file = nested_dir / f"{wid}.jpg"
    img_file.write_text("dummy_image_payload")
    
    # Initialize the webserver
    config = {"database": {"path": db_path}, "daemon": {"target_appids": [294100]}}
    init_webserver(db_path, config)
    
    client = app.test_client()
    
    # Test flat request (gets transparently resolved to nested path)
    resp = client.get(f"/images/{wid}.jpg")
    assert resp.status_code == 200
    assert resp.data == b"dummy_image_payload"
    
    # Test nested request directly (gets served directly)
    resp_nested = client.get(f"/images/{char1}/{char2}/{char3}/{wid}.jpg")
    assert resp_nested.status_code == 200
    assert resp_nested.data == b"dummy_image_payload"
    
    # Test 404
    resp_404 = client.get("/images/non_existent.jpg")
    assert resp_404.status_code == 404
