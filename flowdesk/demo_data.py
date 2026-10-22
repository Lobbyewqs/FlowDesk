import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .database import dumps, now


def seed_batch(service, count=200, actor="local-demo-import"):
    batch_id = "DEMO-" + uuid4().hex[:10].upper()
    prefix = str(secrets.randbelow(900000) + 100000)
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    expired = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    products = [("无线耳机", 29900), ("桌面支架", 6900), ("机械键盘", 45900), ("蓝牙音箱", 19900)]
    with service.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO demo_batches VALUES (?,?,?,?)", (batch_id, count, actor, now()))
        for index in range(count):
            scenario = index % 8
            customer_id = "C001" if (index + index // 8) % 2 == 0 else "C002"
            owner = ("C002" if customer_id == "C001" else "C001") if scenario == 5 else customer_id
            order_id = "A" + prefix + f"{index:03}"
            product, amount = products[index % len(products)]
            descriptions = [f"订单{order_id}的{product}坏了，我想换货。",
                f"订单{order_id}的{product}不想要了，申请退货。", f"订单{order_id}还没收到，请查询物流。",
                f"订单{order_id}买了很久，现在申请退货。", f"我买的{product}坏了，希望换货，订单号还在找。",
                f"订单{order_id}的{product}想换货。", f"订单{order_id}请帮我开发票。", f"订单{order_id}的{product}想退货。"]
            status = "shipped" if scenario in {2, 7} else "delivered"
            delivered = None if status == "shipped" else expired if scenario == 3 else recent
            tracking = "运输中：已到达本地分拨中心" if status == "shipped" else "已签收"
            db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", (order_id, owner, product, amount, status, delivered, tracking))
            ticket_id = "FD-" + uuid4().hex[:10].upper()
            snapshot = {"mode": "demo", "model": "", "demo_scenario": scenario,
                        "demo_order_id": order_id if scenario == 4 else None}
            db.execute("INSERT INTO tickets(id,customer_id,description,status,snapshot,created_at,updated_at,batch_id) VALUES (?,?,?,?,?,?,?,?)",
                       (ticket_id, customer_id, descriptions[scenario], "queued", dumps(snapshot), now(), now(), batch_id))
            service.jobs.insert(db, ticket_id, "start", {"actor": actor})
            db.execute("INSERT INTO demo_cases VALUES (?,?,?)", (ticket_id, scenario, order_id))
    return {"batch_id": batch_id, "count": count, "mode": "demo"}
