The problem is to schedule a tournament of \( n \) teams over \( n-1 \) weeks, with each week divided into \( n/2 \)
periods, and each period divided into two slots. The first team in each slot plays at home, whilst the second plays
the first team away. A tournament must satisfy the following three constraints: every team plays once a week; every
team plays at most twice in the same period over the tournament; every team plays every other team.

Print the home and away schedules (home, away) as lists of lists, where, e.g. home[w][p] is the team playing
home in week w and period p. The teams are numbered from 1 to n.
