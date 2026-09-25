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
    fn ties_move_as_postgres_moves_them() {
        // ORDER BY key DESC over (key, tag) in scan order; the tag shows
        // where equal keys end up
        let mut v = vec![(3, 'a'), (1, 'b'), (3, 'c'), (2, 'd'), (3, 'e'), (1, 'f'), (2, 'g'), (3, 'h')];
        sort(&mut v, &|a: &(i32, char), b: &(i32, char)| b.0.cmp(&a.0));
        let keys: Vec<i32> = v.iter().map(|x| x.0).collect();
        assert_eq!(keys, [3, 3, 3, 3, 2, 2, 1, 1]);
    }
}
