= Reactive Execution Demo

This document illustrates the *reactive* (Pluto-style) DAG execution.

== Chain A → B → C

```python
a = 10
print(f"Cell A: a = {a}")
```

```python
b = a + 1
print(f"Cell B: b = a + 1 = {b}")
```

```python
c = b * 2
print(f"Cell C: c = b * 2 = {c}")
```

== Independent branch D

Cell D does not depend on A, B, or C.  It will *not* re-execute when
you modify Cell A.

```python
import math
d = math.pi
print(f"Cell D (independent): π ≈ {d:.6f}")
```

== Try it yourself

1. Change the value of `a` in Cell A (e.g. ``a = 99``).
2. Run ``typst_pyexec build reactive.typ``.
3. Observe that Cell D is served from cache while A, B, C are re-executed.
