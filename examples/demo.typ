= typst_pyexec Example — Python Output, Plots and Tables

#set document(title: "typst_pyexec Demo", author: "typst_pyexec")
#set page(numbering: "1")

== Scalar and array output

```python
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
print("Libraries loaded.")
```

```python
arr = np.arange(10)
print(arr)
```

== Reactive dependency demo

If you change `cell_scalar` below, only `cell_derived` will re-execute.
`cell_array` (no dependency on `x`) stays cached.

```python
x = 7
print(f"x = {x}")
```

```python
y = x ** 2
print(f"y = x**2 = {y}")
```

== Simple plot

```python
%| label: fig-trig
fig, ax = plt.subplots()
t = np.linspace(0, 2 * np.pi, 200)
ax.plot(t, np.sin(t), label="sin(t)")
ax.plot(t, np.cos(t), label="cos(t)")
ax.legend()
ax.set_title("Trigonometric functions")
plt.show()
```

== Multi-panel figure (subfigures)

```python
%| img-width: 100%
%| label: fig-multipanel
fig, axes = plt.subplots(1, 2, figsize=(8, 3))
axes[0].plot([1, 2, 3], [1, 4, 8])
axes[0].set_title("Quadratic")
axes[1].bar(["A", "B", "C"], [3, 1, 4])
axes[1].set_title("Bar chart")
plt.tight_layout()
plt.show()
```

== Pandas DataFrame table

```python
df = pd.DataFrame({
    "Name": ["Alice", "Bo", "Carol"],
    "Score": [95, 87, 92],
    "Grade": ["A", "B+", "A-"],
})
df
```

== Adding a new cell — no recomputation of previous cells

The cell below is independent of all prior cells.  If you add it
to an existing document, typst_pyexec will execute only *this* cell because
all previous cells are served from the SHA-256 cache.

```python
msg = "I was added later and ran independently!"
print(msg)
print("Coucou")
```

```python
test = np.arange(5)
print("Coucou")
print(test)
```

```python
%| label: test
plt.figure()
plt.title("Gnyajaja")
plt.plot(np.arange(6), np.arange(6))
plt.show()
```

Here is some coucou in @test and in @fig-multipanel-a
The code is amazing because the rebuild is very fast
