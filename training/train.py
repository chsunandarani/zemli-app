"""Train one disease model per crop and export it for the backend and the phone.

Folder layout (class folder names must match the keys in backend/app/disease_info.json):
    data/<crop>/train/<class>/*.jpg
    data/<crop>/val/<class>/*.jpg
    data/<crop>/test_public/<class>/*.jpg   (optional: held-out public dataset images)
    data/<crop>/test_field/<class>/*.jpg    (optional: pilot-farm photos NEVER used in training)

Example:
    python train.py --crop tomato --data data/tomato --epochs 15
Outputs (copy the folder to backend/models/<crop>/):
    out/<crop>/model.onnx, labels.txt, metrics.json
"""
import argparse
import json
import time
from collections import Counter
from pathlib import Path

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, WeightedRandomSampler
from torchvision import datasets, models, transforms

MEAN, STD = [0.485, 0.456, 0.406], [0.229, 0.224, 0.225]


def build_model(arch: str, n_classes: int) -> nn.Module:
    if arch == "efficientnet_b0":
        m = models.efficientnet_b0(weights=models.EfficientNet_B0_Weights.IMAGENET1K_V1)
        m.classifier[1] = nn.Linear(m.classifier[1].in_features, n_classes)
    elif arch == "mobilenet_v3_large":
        m = models.mobilenet_v3_large(weights=models.MobileNet_V3_Large_Weights.IMAGENET1K_V2)
        m.classifier[3] = nn.Linear(m.classifier[3].in_features, n_classes)
    else:
        raise ValueError(arch)
    return m


@torch.no_grad()
def evaluate(model, loader, device, classes):
    model.eval()
    n = len(classes)
    conf = torch.zeros(n, n, dtype=torch.long)
    for x, y in loader:
        pred = model(x.to(device)).argmax(1).cpu()
        for t, p in zip(y, pred):
            conf[t, p] += 1
    total = conf.sum().item()
    acc = conf.diag().sum().item() / max(total, 1)
    per_class = {}
    for i, c in enumerate(classes):
        tp = conf[i, i].item()
        support = conf[i].sum().item()
        predicted = conf[:, i].sum().item()
        prec = tp / predicted if predicted else 0.0
        rec = tp / support if support else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        per_class[c] = {"precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4), "support": support}
    macro_f1 = sum(v["f1"] for v in per_class.values() if v["support"]) / max(1, sum(1 for v in per_class.values() if v["support"]))
    return {"images": total, "accuracy": round(acc, 4), "macro_f1": round(macro_f1, 4),
            "per_class": per_class, "confusion_matrix": conf.tolist()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--crop", required=True, choices=["tomato", "cucumber", "coriander", "gate"],
                    help='"gate" trains the crop-check model (tomato / cucumber / coriander / other)')
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="out")
    ap.add_argument("--arch", default="efficientnet_b0", choices=["efficientnet_b0", "mobilenet_v3_large"])
    ap.add_argument("--epochs", type=int, default=15)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--workers", type=int, default=2)
    args = ap.parse_args()

    data = Path(args.data)
    out = Path(args.out) / args.crop
    out.mkdir(parents=True, exist_ok=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"

    crops_info = json.loads((Path(__file__).parent.parent / "backend/app/disease_info.json")
                            .read_text(encoding="utf-8"))["crops"]
    known = (set(crops_info) | {"other"}) if args.crop == "gate" else set(crops_info[args.crop]["classes"])

    train_tf = transforms.Compose([
        transforms.RandomResizedCrop(224, scale=(0.6, 1.0)),
        transforms.RandomHorizontalFlip(), transforms.RandomVerticalFlip(),
        transforms.RandomRotation(20),
        transforms.ColorJitter(0.3, 0.3, 0.3, 0.05),   # field light varies a lot inside net-sheds
        transforms.ToTensor(), transforms.Normalize(MEAN, STD),
    ])
    eval_tf = transforms.Compose([transforms.Resize(256), transforms.CenterCrop(224),
                                  transforms.ToTensor(), transforms.Normalize(MEAN, STD)])

    train_ds = datasets.ImageFolder(data / "train", train_tf)
    val_ds = datasets.ImageFolder(data / "val", eval_tf)
    classes = train_ds.classes
    unknown = [c for c in classes if c not in known]
    if unknown:
        raise SystemExit(f"Folder names {unknown} are not in disease_info.json for {args.crop}. Rename them.")
    if val_ds.classes != classes:
        raise SystemExit("train/ and val/ must contain exactly the same class folders.")

    counts = Counter(train_ds.targets)
    print("train images per class:", {classes[k]: v for k, v in sorted(counts.items())})
    weights = [1.0 / counts[t] for t in train_ds.targets]  # balance rare diseases
    sampler = WeightedRandomSampler(weights, num_samples=len(weights), replacement=True)
    train_dl = DataLoader(train_ds, args.batch, sampler=sampler, num_workers=args.workers)
    val_dl = DataLoader(val_ds, args.batch, num_workers=args.workers)

    model = build_model(args.arch, len(classes)).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs)
    loss_fn = nn.CrossEntropyLoss(label_smoothing=0.1)

    best_f1, best_path = -1.0, out / "best.pt"
    for epoch in range(1, args.epochs + 1):
        model.train()
        t0, running = time.time(), 0.0
        for x, y in train_dl:
            x, y = x.to(device), y.to(device)
            opt.zero_grad()
            loss = loss_fn(model(x), y)
            loss.backward()
            opt.step()
            running += loss.item() * x.size(0)
        sched.step()
        val = evaluate(model, val_dl, device, classes)
        print(f"epoch {epoch:02d}  loss {running/len(train_ds):.4f}  val acc {val['accuracy']:.4f}  "
              f"val macro-F1 {val['macro_f1']:.4f}  ({time.time()-t0:.0f}s)")
        if val["macro_f1"] > best_f1:
            best_f1 = val["macro_f1"]
            torch.save(model.state_dict(), best_path)

    model.load_state_dict(torch.load(best_path, map_location=device))
    metrics = {"crop": args.crop, "arch": args.arch, "classes": classes,
               "train_images": len(train_ds), "val": evaluate(model, val_dl, device, classes)}
    # Report each test set separately. The field number is the honest real-world one.
    for split in ["test_public", "test_field"]:
        if (data / split).exists():
            ds = datasets.ImageFolder(data / split, eval_tf)
            if ds.classes != classes:
                # map by name so a test set missing some classes still works
                remap = {i: classes.index(c) for i, c in enumerate(ds.classes)}
                ds.targets = [remap[t] for t in ds.targets]
                ds.samples = [(p, remap[t]) for p, t in ds.samples]
            metrics[split] = evaluate(model, DataLoader(ds, args.batch, num_workers=args.workers), device, classes)
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2))

    model.eval().cpu()
    export_kwargs = dict(input_names=["input"], output_names=["logits"], opset_version=17,
                         dynamic_axes={"input": {0: "batch"}, "logits": {0: "batch"}})
    try:  # newer PyTorch defaults to the dynamo exporter, which needs extra packages
        torch.onnx.export(model, torch.randn(1, 3, 224, 224), out / "model.onnx", dynamo=False, **export_kwargs)
    except TypeError:  # older PyTorch has no dynamo argument
        torch.onnx.export(model, torch.randn(1, 3, 224, 224), out / "model.onnx", **export_kwargs)
    (out / "labels.txt").write_text("\n".join(classes) + "\n")
    print(f"\nSaved {out/'model.onnx'} and labels.txt")
    for k in ["val", "test_public", "test_field"]:
        if k in metrics:
            print(f"{k:12s} accuracy {metrics[k]['accuracy']:.4f}  macro-F1 {metrics[k]['macro_f1']:.4f}  "
                  f"({metrics[k]['images']} images)")


if __name__ == "__main__":
    main()
