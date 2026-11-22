import os
from functools import lru_cache

from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import URL, create_engine, text
from sqlalchemy.exc import DBAPIError

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev")

READING_STATUSES = ("Want to Read", "Currently Reading", "Completed")
CLUB_STATUSES = ("Suggested", "Currently Reading", "Completed")
SCORES = [x / 2 for x in range(11)]

RECOMMENDATIONS = """
WITH members AS (
    SELECT userID FROM Member_Of WHERE clubID = :club_id
), genre_taste AS (
    SELECT b.genre, AVG(r.bookScore) AS taste
    FROM Reviews r JOIN Books b ON b.isbn = r.isbn
    WHERE r.userID IN (SELECT userID FROM members)
    GROUP BY b.genre
), member_reading AS (
    SELECT isbn,
           COUNT(*) FILTER (WHERE status = 'Want to Read') AS wishers,
           COUNT(*) FILTER (WHERE status <> 'Want to Read') AS readers
    FROM Tracks_Reading
    WHERE userID IN (SELECT userID FROM members)
    GROUP BY isbn
)
SELECT b.isbn, b.title, b.genre,
       ROUND(COALESCE(t.taste, 0)
             + CASE WHEN LOWER(b.genre) = LOWER(c.genre) THEN 2 ELSE 0 END
             + COALESCE(m.wishers, 0)
             - 2.0 * COALESCE(m.readers, 0) / GREATEST((SELECT COUNT(*) FROM members), 1), 2) AS score
FROM Books b
JOIN Clubs c ON c.clubID = :club_id
LEFT JOIN genre_taste t ON t.genre = b.genre
LEFT JOIN member_reading m ON m.isbn = b.isbn
WHERE b.isbn NOT IN (SELECT isbn FROM Reads WHERE clubID = :club_id)
ORDER BY score DESC, b.title
LIMIT 10
"""


@lru_cache(maxsize=1)
def get_engine():
    return create_engine(
        URL.create(
            "postgresql+psycopg2",
            username=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
            host=os.environ.get("DB_HOST", "127.0.0.1"),
            port=int(os.environ.get("DB_PORT", "5454")),
            database=os.environ.get("DB_NAME", "proj1part2"),
        ),
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=True,
        pool_timeout=5,
        connect_args={"connect_timeout": 5, "options": "-c statement_timeout=5000"},
    )


def db():
    if "conn" not in g:
        g.conn = get_engine().connect()
    return g.conn


def rows(sql, **params):
    return db().execute(text(sql), params).all()


def row(sql, **params):
    return db().execute(text(sql), params).first()


def run(sql, **params):
    db().execute(text(sql), params)


def next_id(table, column, prefix, digits):
    return row(
        f"""
        SELECT :prefix || LPAD((COALESCE(MAX(SUBSTRING({column} FROM 2)::int), 0) + 1)::text, :digits, '0')
        FROM {table} WHERE {column} ~ '^.[0-9]+$'
        """,
        prefix=prefix, digits=digits,
    )[0]


def field(name, max_length, required=False):
    value = request.form.get(name, "").strip()
    if required and not value:
        abort(400, f"{name} is required.")
    if len(value) > max_length:
        abort(400, f"{name} must be at most {max_length} characters.")
    return value or None


def int_field(name):
    value = request.form.get(name, "").strip()
    if not value:
        return None
    if not value.isdigit():
        abort(400, f"{name} must be a whole number.")
    return int(value)


def choice(name, options):
    value = request.form.get(name)
    if value not in options:
        abort(400, f"Invalid {name}.")
    return value


def require_user():
    if g.user is None:
        abort(401, "Log in first.")
    return g.user


def found(record):
    if record is None:
        abort(404)
    return record


@app.before_request
def load_user():
    g.user = None
    if "user_id" in session and request.endpoint not in {"static", "another", "database_check"}:
        g.user = row("SELECT * FROM Users WHERE userID = :id", id=session["user_id"])


@app.teardown_request
def close_connection(exception):
    conn = g.pop("conn", None)
    if conn is not None:
        conn.close()


@app.errorhandler(DBAPIError)
def database_error(error):
    if "conn" in g:
        g.conn.rollback()
    app.logger.warning("Database operation failed (%s)", type(error).__name__)
    if request.endpoint == "database_check":
        return jsonify(database="unavailable"), 503
    message = str(error.orig).splitlines()[0] if error.orig else "Database unavailable."
    return render_template("error.html", message=message), 400


@app.route("/")
def index():
    return redirect(url_for("books"))


@app.route("/another")
def another():
    return render_template("another.html")


@app.route("/db-check")
def database_check():
    if not (os.environ.get("DB_USER") and os.environ.get("DB_PASSWORD")):
        return jsonify(database="unavailable"), 503
    conn = db()
    value = conn.execute(text("SELECT :value"), {"value": 1}).scalar_one()
    schema_accessible = conn.execute(text("""
        SELECT current_schema() IS NOT NULL
            AND has_schema_privilege(current_schema(), 'USAGE')
    """)).scalar_one()
    table_exists = conn.execute(text("SELECT to_regclass('test') IS NOT NULL")).scalar_one()
    return jsonify(
        database="ok",
        select_1=value,
        current_schema_accessible=bool(schema_accessible),
        demo_table_exists=bool(table_exists),
        revision=os.environ.get("K_REVISION", "local"),
    )


@app.route("/books")
def books():
    title, author, genre = (request.args.get(k, "").strip() for k in ("title", "author", "genre"))
    results = rows(
        """
        SELECT DISTINCT b.isbn, b.title, b.pubYear, b.genre
        FROM Books b
        LEFT JOIN Written_By w ON w.isbn = b.isbn
        LEFT JOIN Authors a ON a.authorID = w.authorID
        WHERE b.title ILIKE :title
          AND CONCAT_WS(' ', a.firstName, a.lastName) ILIKE :author
          AND (:genre = '' OR b.genre = :genre)
        ORDER BY b.title
        LIMIT 100
        """,
        title=f"%{title}%", author=f"%{author}%", genre=genre,
    )
    genres = rows("SELECT DISTINCT genre FROM Books WHERE genre IS NOT NULL ORDER BY genre")
    return render_template("books.html", books=results, genres=genres, args=request.args)


@app.route("/books/new", methods=["GET", "POST"])
def new_book():
    if request.method == "GET":
        return render_template("new_book.html")
    isbn = field("isbn", 13, required=True)
    run(
        "INSERT INTO Books (isbn, title, pubYear, description, genre) VALUES (:isbn, :title, :pub_year, :description, :genre)",
        isbn=isbn,
        title=field("title", 200, required=True),
        pub_year=int_field("pubYear"),
        description=field("description", 5000),
        genre=field("genre", 30),
    )
    authors = [a.strip() for a in field("authors", 500, required=True).split(",") if a.strip()]
    for name in authors:
        first, _, last = name.rpartition(" ")
        author = row(
            "SELECT authorID FROM Authors WHERE firstName IS NOT DISTINCT FROM :first AND lastName = :last",
            first=first or None, last=last,
        )
        author_id = author.authorid if author else next_id("Authors", "authorID", "A", 4)
        if not author:
            run("INSERT INTO Authors (authorID, firstName, lastName) VALUES (:id, :first, :last)", id=author_id, first=first or None, last=last)
        run("INSERT INTO Written_By (isbn, authorID) VALUES (:isbn, :id) ON CONFLICT DO NOTHING", isbn=isbn, id=author_id)
    db().commit()
    return redirect(url_for("book", isbn=isbn))


@app.route("/books/<isbn>")
def book(isbn):
    return render_template(
        "book.html",
        book=found(row(
            """
            SELECT b.*, ROUND(AVG(r.bookScore), 2) AS avg_score, COUNT(r.reviewID) AS review_count
            FROM Books b LEFT JOIN Reviews r ON r.isbn = b.isbn
            WHERE b.isbn = :isbn
            GROUP BY b.isbn
            """,
            isbn=isbn,
        )),
        authors=rows(
            "SELECT a.* FROM Authors a JOIN Written_By w ON w.authorID = a.authorID WHERE w.isbn = :isbn",
            isbn=isbn,
        ),
        reviews=rows(
            """
            SELECT r.*, u.username FROM Reviews r JOIN Users u ON u.userID = r.userID
            WHERE r.isbn = :isbn ORDER BY r.reviewDate DESC
            """,
            isbn=isbn,
        ),
        tracking=g.user and row(
            "SELECT status FROM Tracks_Reading WHERE userID = :user_id AND isbn = :isbn",
            user_id=g.user.userid, isbn=isbn,
        ),
        statuses=READING_STATUSES,
        scores=SCORES,
    )


@app.route("/books/<isbn>/track", methods=["POST"])
def track(isbn):
    user = require_user()
    status = request.form.get("status")
    if status == "Remove":
        run("DELETE FROM Tracks_Reading WHERE userID = :user_id AND isbn = :isbn", user_id=user.userid, isbn=isbn)
    else:
        run(
            """
            INSERT INTO Tracks_Reading (userID, isbn, status) VALUES (:user_id, :isbn, :status)
            ON CONFLICT (userID, isbn) DO UPDATE SET status = EXCLUDED.status
            """,
            user_id=user.userid, isbn=isbn, status=choice("status", READING_STATUSES),
        )
    db().commit()
    return redirect(url_for("book", isbn=isbn))


@app.route("/books/<isbn>/reviews", methods=["POST"])
def review(isbn):
    user = require_user()
    run(
        """
        INSERT INTO Reviews (reviewID, bookScore, description, reviewDate, userID, isbn)
        VALUES (:id, :score, :description, CURRENT_DATE, :user_id, :isbn)
        """,
        id=next_id("Reviews", "reviewID", "R", 7),
        score=float(choice("score", [str(s) for s in SCORES])),
        description=field("description", 5000),
        user_id=user.userid,
        isbn=isbn,
    )
    db().commit()
    return redirect(url_for("book", isbn=isbn))


@app.route("/authors/<author_id>")
def author(author_id):
    return render_template(
        "author.html",
        author=found(row("SELECT * FROM Authors WHERE authorID = :id", id=author_id)),
        books=rows(
            "SELECT b.* FROM Books b JOIN Written_By w ON w.isbn = b.isbn WHERE w.authorID = :id ORDER BY b.pubYear",
            id=author_id,
        ),
    )


@app.route("/users")
def users():
    return render_template(
        "users.html",
        users=rows(
            """
            SELECT u.*, un.universityName FROM Users u
            JOIN Universities un ON un.universityID = u.universityID
            ORDER BY u.username
            """
        ),
        universities=rows("SELECT * FROM Universities ORDER BY universityName"),
    )


@app.route("/users", methods=["POST"])
def register():
    user_id = next_id("Users", "userID", "U", 4)
    run(
        """
        INSERT INTO Users (userID, name, username, major, gradYear, universityID)
        VALUES (:id, :name, :username, :major, :grad_year, :university_id)
        """,
        id=user_id,
        name=field("name", 30, required=True),
        username=field("username", 25, required=True),
        major=field("major", 30),
        grad_year=int_field("gradYear"),
        university_id=field("universityID", 6, required=True),
    )
    db().commit()
    session["user_id"] = user_id
    return redirect(url_for("user", user_id=user_id))


@app.route("/login", methods=["POST"])
def login():
    session["user_id"] = found(row("SELECT userID FROM Users WHERE userID = :id", id=request.form.get("userID"))).userid
    return redirect(url_for("books"))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("books"))


@app.route("/users/<user_id>")
def user(user_id):
    return render_template(
        "user.html",
        user=found(row(
            """
            SELECT u.*, un.universityName FROM Users u
            JOIN Universities un ON un.universityID = u.universityID
            WHERE u.userID = :id
            """,
            id=user_id,
        )),
        readings=rows(
            """
            SELECT b.isbn, b.title, t.status FROM Tracks_Reading t JOIN Books b ON b.isbn = t.isbn
            WHERE t.userID = :id ORDER BY t.status, b.title
            """,
            id=user_id,
        ),
        reviews=rows(
            """
            SELECT r.*, b.title FROM Reviews r JOIN Books b ON b.isbn = r.isbn
            WHERE r.userID = :id ORDER BY r.reviewDate DESC
            """,
            id=user_id,
        ),
        clubs=rows(
            """
            SELECT c.clubID, c.clubName, c.presidentID = :id AS is_president
            FROM Clubs c JOIN Member_Of m ON m.clubID = c.clubID
            WHERE m.userID = :id ORDER BY c.clubName
            """,
            id=user_id,
        ),
    )


@app.route("/universities")
def universities():
    name = request.args.get("name", "").strip()
    results = rows(
        """
        SELECT un.*,
                (SELECT COUNT(*) FROM Users u WHERE u.universityID = un.universityID) AS students,
                (SELECT COUNT(*) FROM Clubs c WHERE c.universityID = un.universityID) AS clubs
        FROM Universities AS un
        WHERE un.universityName ILIKE :name 
        ORDER BY un.universityName
        """, 
        name = f"%{name}%", 
    )
    return render_template(
        "universities.html",
        universities = results, 
        args = request.args)

@app.route("/universities/<university_id>")
def university(university_id):
    return render_template(
        "university.html",
        university=found(row("SELECT * FROM Universities WHERE universityID = :id", id=university_id)),
        students=rows("SELECT * FROM Users WHERE universityID = :id ORDER BY username", id=university_id),
        clubs=rows("SELECT * FROM Clubs WHERE universityID = :id ORDER BY clubName", id=university_id),
    )


@app.route("/clubs")
def clubs():
    return render_template(
        "clubs.html",
        clubs=rows(
            """
            SELECT c.*, un.universityName, u.username AS president, COUNT(m.userID) AS members
            FROM Clubs c
            JOIN Universities un ON un.universityID = c.universityID
            JOIN Users u ON u.userID = c.presidentID
            LEFT JOIN Member_Of m ON m.clubID = c.clubID
            GROUP BY c.clubID, un.universityName, u.username
            ORDER BY c.clubName
            """
        ),
        universities=rows("SELECT * FROM Universities ORDER BY universityName"),
    )


@app.route("/clubs", methods=["POST"])
def create_club():
    user = require_user()
    club_id = next_id("Clubs", "clubID", "C", 4)
    run(
        """
        INSERT INTO Clubs (clubID, clubName, genre, description, universityID, presidentID)
        VALUES (:id, :name, :genre, :description, :university_id, :president_id)
        """,
        id=club_id,
        name=field("clubName", 100, required=True),
        genre=field("genre", 30),
        description=field("description", 5000),
        university_id=field("universityID", 6, required=True),
        president_id=user.userid,
    )
    run("INSERT INTO Member_Of (userID, clubID) VALUES (:user_id, :club_id)", user_id=user.userid, club_id=club_id)
    db().commit()
    return redirect(url_for("club", club_id=club_id))


@app.route("/clubs/<club_id>")
def club(club_id):
    club = found(row(
        """
        SELECT c.*, un.universityName, u.username AS president FROM Clubs c
        JOIN Universities un ON un.universityID = c.universityID
        JOIN Users u ON u.userID = c.presidentID
        WHERE c.clubID = :id
        """,
        id=club_id,
    ))
    members = rows(
        "SELECT u.* FROM Users u JOIN Member_Of m ON m.userID = u.userID WHERE m.clubID = :id ORDER BY u.username",
        id=club_id,
    )
    return render_template(
        "club.html",
        club=club,
        members=members,
        is_member=g.user is not None and any(m.userid == g.user.userid for m in members),
        is_president=g.user is not None and club.presidentid == g.user.userid,
        books=rows(
            """
            SELECT b.isbn, b.title, r.status FROM Reads r JOIN Books b ON b.isbn = r.isbn
            WHERE r.clubID = :id ORDER BY r.status, b.title
            """,
            id=club_id,
        ),
        recommendations=rows(RECOMMENDATIONS, club_id=club_id),
        statuses=CLUB_STATUSES,
    )


@app.route("/clubs/<club_id>/join", methods=["POST"])
def join_club(club_id):
    run(
        "INSERT INTO Member_Of (userID, clubID) VALUES (:user_id, :club_id) ON CONFLICT DO NOTHING",
        user_id=require_user().userid, club_id=club_id,
    )
    db().commit()
    return redirect(url_for("club", club_id=club_id))


@app.route("/clubs/<club_id>/leave", methods=["POST"])
def leave_club(club_id):
    run(
        """
        DELETE FROM Member_Of
        WHERE userID = :user_id AND clubID = :club_id
          AND userID <> (SELECT presidentID FROM Clubs WHERE clubID = :club_id)
        """,
        user_id=require_user().userid, club_id=club_id,
    )
    db().commit()
    return redirect(url_for("club", club_id=club_id))


@app.route("/clubs/<club_id>/books", methods=["POST"])
def set_club_book(club_id):
    president = row("SELECT presidentID FROM Clubs WHERE clubID = :id", id=club_id)
    if found(president).presidentid != require_user().userid:
        abort(403, "Only the club president can choose books.")
    run(
        """
        INSERT INTO Reads (clubID, isbn, status) VALUES (:club_id, :isbn, :status)
        ON CONFLICT (clubID, isbn) DO UPDATE SET status = EXCLUDED.status
        """,
        club_id=club_id, isbn=request.form.get("isbn"), status=choice("status", CLUB_STATUSES),
    )
    db().commit()
    return redirect(url_for("club", club_id=club_id))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8111")))
