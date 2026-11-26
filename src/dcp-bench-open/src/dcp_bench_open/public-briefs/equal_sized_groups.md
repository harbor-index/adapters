I have a sorted list of N elements, and I want to divide
them into K groups, by choosing K-1 split points between them. There may be
elements with the same value, and it is necessary to have items with same value in
the same group. Find K groups as close in size to round(N/K) as possible.

For example, divide these 32 elements into 4 groups, as close to 8 elements in each group as possible (32/4=8):

1 1 1 1 2 2 3 3 3 3 3 3 3 3 4 4 4 4 5 5 5 5 5 6 6 6 6 7 8 9 10 10

Minimize the sum of absolute deviations of group sizes from round(N/K).

Print - for the data given below - the 1-based indexes of the break points (x) such that the total error is minimized.
