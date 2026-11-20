# Book Clubs — COMS W4111 Project 1 Part 3

Elizabeth Korzilius, Trevor Semeraro

- PostgreSQL account: `ejk2200`
- Cloud Run URL: `TODO`

## Implemented from the Part 1 proposal

- Register with a university, log in as a user, profile with university, major, reading list, reviews and clubs.
- Track books as Want to Read / Currently Reading / Completed.
- Rate (0–5 in 0.5 steps) and review books.
- Search books by title, author and genre; add missing books with their authors.
- Browse universities, authors, users and clubs.
- Create a club (creator becomes president and member), join and leave clubs.
- Club recommendations: 10 suggestions ranked from members' reading histories and genre ratings; the president picks books and sets their status.

## Interesting database operations

1. **Club page** (`/clubs/<id>`): the recommendation query averages members' review scores per genre, adds a bonus for the club's genre and for books members want to read, subtracts a penalty proportional to the share of members who have already read the book, and excludes books already on the club's list.
2. **Creating a club** (`POST /clubs`): inserts the club and the president's membership in one transaction. A deferred foreign key `(presidentID, clubID) → Member_Of` enforces that the president is a member, so the president also cannot leave their own club.

## AI usage

Elizabeth: 
I used ChatGPT to help keep track of how we implemented the features from our Part 1 Proposal. I also used it to help me debug code and figure out which terminal commands to run when I got stuck (starting the server, pushing changes to Git, etc.).
