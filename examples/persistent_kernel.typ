= Persistent Kernel Demo

Imports defined in an earlier cell are available in all later cells
because the Jupyter kernel's namespace is preserved across builds.

```python
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
print("numpy version:", np.__version__)
```

```python
# np is already in the kernel namespace from the `imports` cell.
result = np.sqrt(np.array([1, 4, 9, 16, 25]))
print("Square roots:", result)
```

```python
%| caption: Matplotlib reuses imported namespace
x = np.linspace(0, 10, 300)
plt.plot(x, np.exp(-0.3 * x) * np.sin(x))
plt.title("Damped oscillation")
plt.xlabel("t")
plt.ylabel("f(t)")
plt.show()
```

== Adding a new cell later

If you add the cell below *after* an initial build, typst_pyexec will:

1. Detect that the new cell is not in the cache.
2. Execute only the new cell (imports cell is cached — kernel already has `np`).
3. Re-use the live kernel namespace so `np` is available immediately.

```python
# This cell was added in a second build but imports still work.
arr = np.random.default_rng(0).integers(0, 100, size=5)
print("Random integers:", arr)
```
