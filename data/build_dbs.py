"""Build pre- and post-perturbation SQLite databases for the SITWE 2026 pilot.

The pre database mirrors Spider's ``concert_singer`` (four tables: stadium,
singer, concert, singer_in_concert). The post database applies a deterministic
EvoSchema-style perturbation:

* TABLE_RENAME : ``singer`` -> ``artist``
* COLUMN_RENAME: ``singer.Song_release_year`` -> ``artist.debut_year``
                 ``singer.Song_Name``         -> ``artist.song_title``

Run::

    python data/build_dbs.py

Outputs ``concert_singer_pre.sqlite`` and ``concert_singer_post.sqlite``
into the same directory as this file.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONCERT_SINGER_DIR = HERE / "concert_singer"
PRE_DB = CONCERT_SINGER_DIR / "concert_singer_pre.sqlite"
POST_DB = CONCERT_SINGER_DIR / "concert_singer_post.sqlite"
HR_DIR = HERE / "hr_1"
HR_PRE_DB = HR_DIR / "hr_1_pre.sqlite"


PRE_SCHEMA = [
    """
    CREATE TABLE stadium (
        Stadium_ID  INTEGER PRIMARY KEY,
        Location    TEXT,
        Name        TEXT,
        Capacity    INTEGER,
        Highest     INTEGER,
        Lowest      INTEGER,
        Average     INTEGER
    );
    """,
    """
    CREATE TABLE singer (
        Singer_ID         INTEGER PRIMARY KEY,
        Name              TEXT,
        Country           TEXT,
        Song_Name         TEXT,
        Song_release_year INTEGER,
        Age               INTEGER,
        Is_male           TEXT
    );
    """,
    """
    CREATE TABLE concert (
        Concert_ID    INTEGER PRIMARY KEY,
        concert_Name  TEXT,
        Theme         TEXT,
        Stadium_ID    INTEGER,
        Year          INTEGER,
        FOREIGN KEY (Stadium_ID) REFERENCES stadium(Stadium_ID)
    );
    """,
    """
    CREATE TABLE singer_in_concert (
        Concert_ID INTEGER,
        Singer_ID  INTEGER,
        PRIMARY KEY (Concert_ID, Singer_ID),
        FOREIGN KEY (Concert_ID) REFERENCES concert(Concert_ID),
        FOREIGN KEY (Singer_ID)  REFERENCES singer(Singer_ID)
    );
    """,
]

POST_SCHEMA = [
    """
    CREATE TABLE stadium (
        Stadium_ID  INTEGER PRIMARY KEY,
        Location    TEXT,
        Name        TEXT,
        Capacity    INTEGER,
        Highest     INTEGER,
        Lowest      INTEGER,
        Average     INTEGER
    );
    """,
    # singer -> artist, with two column renames
    """
    CREATE TABLE artist (
        Singer_ID    INTEGER PRIMARY KEY,
        Name         TEXT,
        Country      TEXT,
        song_title   TEXT,
        debut_year   INTEGER,
        Age          INTEGER,
        Is_male      TEXT
    );
    """,
    """
    CREATE TABLE concert (
        Concert_ID    INTEGER PRIMARY KEY,
        concert_Name  TEXT,
        Theme         TEXT,
        Stadium_ID    INTEGER,
        Year          INTEGER,
        FOREIGN KEY (Stadium_ID) REFERENCES stadium(Stadium_ID)
    );
    """,
    """
    CREATE TABLE singer_in_concert (
        Concert_ID INTEGER,
        Singer_ID  INTEGER,
        PRIMARY KEY (Concert_ID, Singer_ID),
        FOREIGN KEY (Concert_ID) REFERENCES concert(Concert_ID),
        FOREIGN KEY (Singer_ID)  REFERENCES artist(Singer_ID)
    );
    """,
]

STADIUMS = [
    (1, "Raith Rovers",      "Stark's Park",          10104,  4812, 1294, 2106),
    (2, "Ayr United",        "Somerset Park",         11998,  2363, 1057, 1477),
    (3, "East Fife",         "Bayview Stadium",        2000,  1980,  533,  864),
    (4, "Queen's Park",      "Hampden Park",          52500, 1763,  466,  730),
    (5, "Stirling Albion",   "Forthbank Stadium",      3808,  1125,  404,  642),
    (6, "Arbroath",          "Gayfield Park",          4125,  1683,  529,  922),
    (7, "Alloa Athletic",    "Recreation Park",        3100,  1057,  331,  637),
    (8, "Peterhead",         "Balmoor",                3250,  1924,  411,  837),
    (9, "Brechin City",      "Glebe Park",             3960,   780,  315,  552),
]

SINGERS = [
    (1, "Joe Sharp",        "Netherlands",  "You",                       1992, 52, "F"),
    (2, "Timbaland",        "United States","Dangerous",                 2008, 32, "T"),
    (3, "Justin Brown",     "France",       "Hey Oh",                    2013, 29, "T"),
    (4, "Rose White",       "France",       "Sun",                       2003, 41, "F"),
    (5, "John Nizinik",     "France",       "Gentleman",                 2014, 43, "T"),
    (6, "Tribal King",      "France",       "Love",                      2016, 25, "T"),
]

CONCERTS = [
    (1, "Auditions",       "Free choice",  1, 2014),
    (2, "Super bootcamp",  "Free choice 2",2, 2014),
    (3, "Home Visits",     "Bell Boy",     2, 2015),
    (4, "Week 1",          "Reloaded",     10,2014),
    (5, "Week 1",          "Wide Awake",   9, 2015),
    (6, "Week 2",          "Happy Tonight",7, 2015),
]

SINGER_IN_CONCERT = [
    (1, 2), (1, 3), (1, 5),
    (2, 3), (2, 6),
    (3, 5), (3, 4),
    (4, 1), (4, 5),
    (5, 6), (5, 3),
    (6, 2), (6, 1),
]

HR_SCHEMA = [
    """
    CREATE TABLE regions (
        region_id   INTEGER PRIMARY KEY,
        region_name TEXT
    );
    """,
    """
    CREATE TABLE countries (
        country_id   TEXT PRIMARY KEY,
        country_name TEXT,
        region_id    INTEGER,
        FOREIGN KEY (region_id) REFERENCES regions(region_id)
    );
    """,
    """
    CREATE TABLE locations (
        location_id    INTEGER PRIMARY KEY,
        street_address TEXT,
        postal_code    TEXT,
        city           TEXT,
        state_province TEXT,
        country_id     TEXT,
        FOREIGN KEY (country_id) REFERENCES countries(country_id)
    );
    """,
    """
    CREATE TABLE departments (
        department_id   INTEGER PRIMARY KEY,
        department_name TEXT,
        manager_id      INTEGER,
        location_id     INTEGER,
        FOREIGN KEY (manager_id) REFERENCES employees(employee_id),
        FOREIGN KEY (location_id) REFERENCES locations(location_id)
    );
    """,
    """
    CREATE TABLE jobs (
        job_id     TEXT PRIMARY KEY,
        job_title  TEXT,
        min_salary INTEGER,
        max_salary INTEGER
    );
    """,
    """
    CREATE TABLE employees (
        employee_id    INTEGER PRIMARY KEY,
        first_name     TEXT,
        last_name      TEXT,
        email          TEXT,
        phone_number   TEXT,
        hire_date      TEXT,
        job_id         TEXT,
        salary         REAL,
        commission_pct REAL,
        manager_id     INTEGER,
        department_id  INTEGER,
        FOREIGN KEY (job_id) REFERENCES jobs(job_id),
        FOREIGN KEY (manager_id) REFERENCES employees(employee_id),
        FOREIGN KEY (department_id) REFERENCES departments(department_id)
    );
    """,
    """
    CREATE TABLE job_history (
        employee_id   INTEGER,
        start_date    TEXT,
        end_date      TEXT,
        job_id        TEXT,
        department_id INTEGER,
        PRIMARY KEY (employee_id, start_date),
        FOREIGN KEY (employee_id) REFERENCES employees(employee_id),
        FOREIGN KEY (job_id) REFERENCES jobs(job_id),
        FOREIGN KEY (department_id) REFERENCES departments(department_id)
    );
    """,
    """
    CREATE TABLE dependents (
        dependent_id INTEGER PRIMARY KEY,
        first_name   TEXT,
        last_name    TEXT,
        relationship TEXT,
        employee_id  INTEGER,
        FOREIGN KEY (employee_id) REFERENCES employees(employee_id)
    );
    """,
]

HR_REGIONS = [(i, f"Region {i}") for i in range(1, 16)]
HR_COUNTRIES = [
    (f"C{i:02d}", f"Country {i}", ((i - 1) % 5) + 1)
    for i in range(1, 16)
]
HR_LOCATIONS = [
    (
        1000 + i,
        f"{100 + i} Main St",
        f"{10000 + i}",
        f"City{i}",
        f"State{i}",
        HR_COUNTRIES[i - 1][0],
    )
    for i in range(1, 16)
]
HR_DEPARTMENTS = [
    (9 + i, f"Dept {i}", None, HR_LOCATIONS[i - 1][0])
    for i in range(1, 16)
]
HR_JOBS = [
    (f"JOB{i:02d}", f"Job {i}", 3000 + i * 150, 6000 + i * 200)
    for i in range(1, 16)
]
HR_EMPLOYEES = [
    (1, "Alice", "Ng", "ANG", "515.123.1001", "2007-06-17", "JOB01", 8000, None, None, 10),
    (2, "Brian", "Smith", "BSM", "515.123.1002", "2007-06-18", "JOB02", 7800, 0.1, None, 11),
    (3, "Carla", "Diaz", "CDI", "515.123.1003", "2007-06-19", "JOB03", 7600, None, None, 12),
    (4, "Diego", "Khan", "DKH", "515.123.1004", "2007-06-20", "JOB04", 7400, None, 1, 13),
    (5, "Elena", "Ivan", "EIV", "515.123.1005", "2007-06-21", "JOB05", 7200, 0.05, 1, 14),
    (6, "Farah", "Lo", "FLO", "515.123.1006", "2007-06-22", "JOB06", 7000, None, 2, 15),
    (7, "Gavin", "Omar", "GOM", "515.123.1007", "2007-06-23", "JOB07", 6800, None, 2, 16),
    (8, "Hana", "Park", "HPA", "515.123.1008", "2007-06-24", "JOB08", 6600, None, 3, 17),
    (9, "Ivan", "Quinn", "IQU", "515.123.1009", "2007-06-25", "JOB09", 6400, None, 3, 18),
    (10, "Jade", "Rao", "JRA", "515.123.1010", "2007-06-26", "JOB10", 6200, 0.08, 1, 19),
    (11, "Kai", "Singh", "KSI", "515.123.1011", "2007-06-27", "JOB11", 6000, None, 2, 20),
    (12, "Lena", "Tan", "LTA", "515.123.1012", "2007-06-28", "JOB12", 5800, None, 3, 21),
    (13, "Mia", "Umar", "MUM", "515.123.1013", "2007-06-29", "JOB13", 5600, None, 1, 22),
    (14, "Noah", "Vega", "NVE", "515.123.1014", "2007-06-30", "JOB14", 5400, None, 2, 23),
    (15, "Owen", "Wong", "OWO", "515.123.1015", "2007-07-01", "JOB15", 5200, None, 3, 24),
]
HR_JOB_HISTORY = [
    (i, f"2001-01-{i:02d}", f"2002-01-{i:02d}", f"JOB{i:02d}", 9 + i)
    for i in range(1, 16)
]
HR_DEPENDENTS = [
    (i, f"Dep{i}", f"Last{i}", "Child" if i % 2 else "Spouse", i)
    for i in range(1, 16)
]


def _populate(con: sqlite3.Connection) -> None:
    cur = con.cursor()
    # First 6 stadiums; last 3 are unused but mirror Spider's spread.
    cur.executemany(
        "INSERT INTO stadium VALUES (?,?,?,?,?,?,?)",
        STADIUMS,
    )
    cur.executemany(
        # Will be retargeted by table name in build_post().
        f"INSERT INTO {{tbl}} VALUES (?,?,?,?,?,?,?)",
        SINGERS,
    )


def build_pre(path: Path = PRE_DB) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    try:
        for stmt in PRE_SCHEMA:
            con.execute(stmt)
        con.executemany("INSERT INTO stadium VALUES (?,?,?,?,?,?,?)", STADIUMS)
        con.executemany("INSERT INTO singer  VALUES (?,?,?,?,?,?,?)", SINGERS)
        con.executemany("INSERT INTO concert VALUES (?,?,?,?,?)",      CONCERTS)
        con.executemany(
            "INSERT INTO singer_in_concert VALUES (?,?)", SINGER_IN_CONCERT
        )
        con.commit()
    finally:
        con.close()


def build_post(path: Path = POST_DB) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    try:
        for stmt in POST_SCHEMA:
            con.execute(stmt)
        con.executemany("INSERT INTO stadium VALUES (?,?,?,?,?,?,?)", STADIUMS)
        con.executemany("INSERT INTO artist  VALUES (?,?,?,?,?,?,?)", SINGERS)
        con.executemany("INSERT INTO concert VALUES (?,?,?,?,?)",      CONCERTS)
        con.executemany(
            "INSERT INTO singer_in_concert VALUES (?,?)", SINGER_IN_CONCERT
        )
        con.commit()
    finally:
        con.close()


def build_hr_1_pre(path: Path = HR_PRE_DB) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        for stmt in HR_SCHEMA:
            con.execute(stmt)
        con.executemany("INSERT INTO regions VALUES (?,?)", HR_REGIONS)
        con.executemany("INSERT INTO countries VALUES (?,?,?)", HR_COUNTRIES)
        con.executemany("INSERT INTO locations VALUES (?,?,?,?,?,?)", HR_LOCATIONS)
        con.executemany("INSERT INTO departments VALUES (?,?,?,?)", HR_DEPARTMENTS)
        con.executemany("INSERT INTO jobs VALUES (?,?,?,?)", HR_JOBS)
        con.executemany("INSERT INTO employees VALUES (?,?,?,?,?,?,?,?,?,?,?)", HR_EMPLOYEES)
        con.executemany("INSERT INTO job_history VALUES (?,?,?,?,?)", HR_JOB_HISTORY)
        con.executemany("INSERT INTO dependents VALUES (?,?,?,?,?)", HR_DEPENDENTS)
        con.executemany(
            "UPDATE departments SET manager_id=? WHERE department_id=?",
            [(1, 10), (2, 11), (3, 12), (1, 13), (2, 14)],
        )
        con.commit()
    finally:
        con.close()


def main() -> None:
    build_pre()
    build_post()
    build_hr_1_pre()
    print(f"[ok] wrote {PRE_DB.name}  ({os.path.getsize(PRE_DB)} bytes)")
    print(f"[ok] wrote {POST_DB.name} ({os.path.getsize(POST_DB)} bytes)")
    print(f"[ok] wrote {HR_PRE_DB.name} ({os.path.getsize(HR_PRE_DB)} bytes)")


if __name__ == "__main__":
    main()
