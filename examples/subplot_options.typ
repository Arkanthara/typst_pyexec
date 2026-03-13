= Subplot Options Demo

```python
%| label: test
%| grid-align: bottom
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.figure()
plt.suptitle("Test on values of \n $\\phi_1$")

index = 1
for i, j in zip(
    [0.1, 0.5, 0.9, 1.0],
    [
        "Stationary process\n",
        "Mean-reverting process\n",
        "Trendy process\n",
        "Exploding process\n",
    ],
):
    plt.subplot(2, 2, index)
    plt.plot([0, i, i * 2, i * 3])
    plt.title(j + f"$\\phi_1 = {i}$")
    plt.xlabel("Time")
    plt.ylabel("$X_t$")
    index += 1

plt.show()
```
