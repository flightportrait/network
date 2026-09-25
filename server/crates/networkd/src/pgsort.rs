//! PostgreSQL's in-memory sort (src/include/lib/sort_template.h), so that
//! rows a query leaves tied come out in the order the Python service,
//! reading Postgres, shows them.
//!
//! It is Bentley–McIlroy quicksort with Postgres's own touches: insertion
//! sort under seven elements, an early exit when the input is already in
//! order, median-of-three (ninther above forty), and a three-way partition.
//! Not stable: equal rows move, and exactly how is the point of porting it.
//! Fed rows in the order Postgres's scan would give them (heap order,
//! which the snapshot keeps as rowid order), it reproduces its output.

use std::cmp::Ordering;

fn med3<T>(v: &[T], a: usize, b: usize, c: usize, cmp: &impl Fn(&T, &T) -> Ordering) -> usize {
    if cmp(&v[a], &v[b]) == Ordering::Less {
        if cmp(&v[b], &v[c]) == Ordering::Less {
            b
        } else if cmp(&v[a], &v[c]) == Ordering::Less {
            c
        } else {
            a
        }
    } else if cmp(&v[b], &v[c]) == Ordering::Greater {
        b
    } else if cmp(&v[a], &v[c]) == Ordering::Less {
        a
    } else {
        c
    }
}

/// Swap `n` elements starting at `a` with `n` starting at `b`.
fn swapn<T>(v: &mut [T], a: usize, b: usize, n: usize) {
    for i in 0..n {
        v.swap(a + i, b + i);
    }
}

pub fn sort<T>(v: &mut [T], cmp: &impl Fn(&T, &T) -> Ordering) {
    let mut base = 0usize;
    let mut n = v.len();
    loop {
        if n < 7 {
            for pm in base + 1..base + n {
                let mut pl = pm;
                while pl > base && cmp(&v[pl - 1], &v[pl]) == Ordering::Greater {
                    v.swap(pl, pl - 1);
                    pl -= 1;
                }
            }
            return;
        }
        let presorted = (base + 1..base + n).all(|pm| cmp(&v[pm - 1], &v[pm]) != Ordering::Greater);
        if presorted {
            return;
        }
        let mut pm = base + n / 2;
        if n > 7 {
            let mut pl = base;
            let mut pn = base + n - 1;
            if n > 40 {
                let d = n / 8;
                pl = med3(v, pl, pl + d, pl + 2 * d, cmp);
                pm = med3(v, pm - d, pm, pm + d, cmp);
                pn = med3(v, pn - 2 * d, pn - d, pn, cmp);
            }
            pm = med3(v, pl, pm, pn, cmp);
        }
        v.swap(base, pm);
        let (mut pa, mut pb) = (base + 1, base + 1);
        // pc and pd may step below `base` by one; keep them signed
        let (mut pc, mut pd) = ((base + n - 1) as isize, (base + n - 1) as isize);
        loop {
            while pb as isize <= pc {
                let r = cmp(&v[pb], &v[base]);
                if r == Ordering::Greater {
                    break;
                }
                if r == Ordering::Equal {
                    v.swap(pa, pb);
                    pa += 1;
                }
                pb += 1;
            }
            while pb as isize <= pc {
                let r = cmp(&v[pc as usize], &v[base]);
                if r == Ordering::Less {
                    break;
                }
                if r == Ordering::Equal {
                    v.swap(pc as usize, pd as usize);
                    pd -= 1;
                }
                pc -= 1;
            }
            if pb as isize > pc {
                break;
            }
            v.swap(pb, pc as usize);
            pb += 1;
            pc -= 1;
        }
        let pn = base + n;
        let d1 = (pa - base).min(pb - pa);
        swapn(v, base, pb - d1, d1);
        let d1 = ((pd - pc) as usize).min(pn - pd as usize - 1);
        swapn(v, pb, pn - d1, d1);
        let d1 = pb - pa;
        let d2 = (pd - pc) as usize;
        if d1 <= d2 {
            if d1 > 1 {
                sort(&mut v[base..base + d1], cmp);
            }
            if d2 > 1 {
                base = pn - d2;
                n = d2;
                continue;
            }
        } else {
            if d2 > 1 {
                sort(&mut v[pn - d2..pn], cmp);
            }
            if d1 > 1 {
                n = d1;
                continue;
            }
        }
        return;
    }
}

/// `ORDER BY ... LIMIT bound` as Postgres runs it over `v` in scan order:
/// up to 2×bound rows are sorted whole (the quicksort above); past that a
/// bounded heap keeps the best `bound` (a row equal to the worst kept is
/// dropped, so among ties the first scanned stay), and the heap is then
/// unwound into order (tuplesort.c: make_bounded_heap, sort_bounded_heap).
pub fn top_n<T: Clone>(v: Vec<T>, bound: usize, cmp: &impl Fn(&T, &T) -> Ordering) -> Vec<T> {
    let mut v = v;
    if bound == 0 {
        return vec![];
    }
    if v.len() <= bound * 2 {
        sort(&mut v, cmp);
        v.truncate(bound);
        return v;
    }
    // the heap runs in reversed direction: its root is the worst kept row
    let rev = |a: &T, b: &T| cmp(b, a);
    let mut heap: Vec<T> = Vec::with_capacity(bound);
    for t in v {
        if heap.len() < bound {
            heap_insert(&mut heap, t, &rev);
        } else if rev(&t, &heap[0]) != Ordering::Greater {
            // new row <= root in reversed order: no better than the worst kept
        } else {
            replace_top(&mut heap, t, &rev);
        }
    }
    // unwind: each delete-top takes the worst left and stores it past the
    // shrinking heap, which leaves the array in sort order
    let n = heap.len();
    let mut len = n;
    while len > 1 {
        let top = heap[0].clone();
        len -= 1;
        let last = heap[len].clone();
        replace_top(&mut heap[..len], last, &rev);
        heap[len] = top;
    }
    heap
}

fn heap_insert<T>(heap: &mut Vec<T>, t: T, rev: &impl Fn(&T, &T) -> Ordering) {
    heap.push(t);
    let mut j = heap.len() - 1;
    while j > 0 {
        let i = (j - 1) >> 1;
        if rev(&heap[j], &heap[i]) != Ordering::Less {
            break;
        }
        heap.swap(i, j);
        j = i;
    }
}

/// Put `t` at the root and sift it down (tuplesort_heap_replace_top).
fn replace_top<T>(heap: &mut [T], t: T, rev: &impl Fn(&T, &T) -> Ordering) {
    let n = heap.len();
    if n == 0 {
        return;
    }
    heap[0] = t;
    let mut i = 0;
    loop {
        let mut j = 2 * i + 1;
        if j >= n {
            break;
        }
        if j + 1 < n && rev(&heap[j], &heap[j + 1]) == Ordering::Greater {
            j += 1;
        }
        if rev(&heap[i], &heap[j]) != Ordering::Greater {
            break;
        }
        heap.swap(i, j);
        i = j;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rand::{Rng, SeedableRng};

    #[test]
    fn sorts_like_any_sort() {
        let mut rng = rand_chacha::ChaCha8Rng::seed_from_u64(5);
        for n in [0usize, 1, 2, 6, 7, 8, 40, 41, 100, 1000] {
            for _ in 0..20 {
                let mut v: Vec<i32> = (0..n).map(|_| rng.gen_range(0..n.max(1) as i32 / 3 + 1)).collect();
                let mut want = v.clone();
                want.sort();
                sort(&mut v, &|a: &i32, b: &i32| a.cmp(b));
                assert_eq!(v, want, "n={n}");
            }
        }
    }

    #[test]
    fn top_n_is_a_prefix_of_the_sort() {
        let mut rng = rand_chacha::ChaCha8Rng::seed_from_u64(8);
        for n in [0usize, 3, 5, 10, 11, 12, 50, 500] {
            for bound in [1usize, 5, 80] {
                let v: Vec<i32> = (0..n).map(|_| rng.gen_range(0..20)).collect();
                let got = top_n(v.clone(), bound, &|a: &i32, b: &i32| b.cmp(a));
                let mut want = v;
                want.sort_by(|a, b| b.cmp(a));
                want.truncate(bound);
                assert_eq!(got, want, "n={n} bound={bound}");
            }
        }
    }

    #[test]
    fn ties_move_as_postgres_moves_them() {
        // ORDER BY key DESC over (key, tag) in scan order; the tag shows
        // where equal keys end up
        let mut v = vec![(3, 'a'), (1, 'b'), (3, 'c'), (2, 'd'), (3, 'e'), (1, 'f'), (2, 'g'), (3, 'h')];
        sort(&mut v, &|a: &(i32, char), b: &(i32, char)| b.0.cmp(&a.0));
        let keys: Vec<i32> = v.iter().map(|x| x.0).collect();
        assert_eq!(keys, [3, 3, 3, 3, 2, 2, 1, 1]);
    }
}
