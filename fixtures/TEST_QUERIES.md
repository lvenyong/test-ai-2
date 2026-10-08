# PostgreSQL MCP 自然语言查询测试集

本文档包含针对三个测试数据库的自然语言查询示例，用于测试 PostgreSQL MCP 服务器的 SQL 生成能力。查询按难度级别（简单→中等→复杂→高级）组织。

## 目录
- [Small Database (blog_small)](#small-database-blog_small)
- [Medium Database (ecommerce_medium)](#medium-database-ecommerce_medium)
- [Large Database (saas_crm_large)](#large-database-saas_crm_large)
- [跨难度综合测试](#跨难度综合测试)

---

## Small Database (blog_small)

### 📊 数据库结构
- 7 张表：users, posts, comments, categories, tags, post_tags, user_sessions
- 3 个视图：published_posts, user_stats, popular_tags
- 测试数据：8 用户, 10 文章, 17 评论

### Level 1: 简单查询 (基础 SELECT)

#### Q1.1 基础数据统计
```
自然语言：有多少用户？
期望 SQL：SELECT COUNT(*) FROM users;
```

#### Q1.2 简单筛选
```
自然语言：显示所有已发布的文章
期望 SQL：SELECT * FROM posts WHERE status = 'published';
```

#### Q1.3 计数查询
```
自然语言：有多少篇草稿文章？
期望 SQL：SELECT COUNT(*) FROM posts WHERE status = 'draft';
```

#### Q1.4 列出数据
```
自然语言：列出所有分类
期望 SQL：SELECT * FROM categories;
```

#### Q1.5 查看最新数据
```
自然语言：显示最新的 5 篇文章
期望 SQL：SELECT * FROM posts ORDER BY created_at DESC LIMIT 5;
```

### Level 2: 中等查询 (JOIN + 聚合)

#### Q2.1 简单 JOIN
```
自然语言：显示所有文章及其作者名称
期望 SQL：
SELECT p.title, u.username, u.full_name
FROM posts p
JOIN users u ON p.author_id = u.id;
```

#### Q2.2 带筛选的 JOIN
```
自然语言：找出 Technology 分类下的所有文章
期望 SQL：
SELECT p.title, p.created_at
FROM posts p
JOIN categories c ON p.category_id = c.id
WHERE c.name = 'Technology';
```

#### Q2.3 聚合统计
```
自然语言：每个作者写了多少篇文章？
期望 SQL：
SELECT u.username, COUNT(p.id) as post_count
FROM users u
LEFT JOIN posts p ON u.id = p.author_id
GROUP BY u.id, u.username
ORDER BY post_count DESC;
```

#### Q2.4 带条件的聚合
```
自然语言：哪些文章的评论数超过 2 条？
期望 SQL：
SELECT p.title, COUNT(c.id) as comment_count
FROM posts p
LEFT JOIN comments c ON p.id = c.post_id
GROUP BY p.id, p.title
HAVING COUNT(c.id) > 2;
```

#### Q2.5 排序聚合
```
自然语言：按阅读量排序显示前 3 篇文章
期望 SQL：
SELECT title, view_count
FROM posts
WHERE status = 'published'
ORDER BY view_count DESC
LIMIT 3;
```

### Level 3: 复杂查询 (多表 JOIN + 子查询)

#### Q3.1 多表关联
```
自然语言：显示每篇文章的标题、作者、分类和评论数
期望 SQL：
SELECT
    p.title,
    u.full_name as author,
    c.name as category,
    COUNT(DISTINCT cm.id) as comment_count
FROM posts p
JOIN users u ON p.author_id = u.id
LEFT JOIN categories c ON p.category_id = c.id
LEFT JOIN comments cm ON p.id = cm.post_id
WHERE p.status = 'published'
GROUP BY p.id, p.title, u.full_name, c.name;
```

#### Q3.2 多对多关系查询
```
自然语言：找出带有"Python"标签的所有文章
期望 SQL：
SELECT p.title, p.published_at
FROM posts p
JOIN post_tags pt ON p.id = pt.post_id
JOIN tags t ON pt.tag_id = t.id
WHERE t.name = 'Python';
```

#### Q3.3 子查询统计
```
自然语言：找出评论数最多的文章
期望 SQL：
SELECT p.title, COUNT(c.id) as comment_count
FROM posts p
LEFT JOIN comments c ON p.id = c.post_id
GROUP BY p.id, p.title
ORDER BY comment_count DESC
LIMIT 1;
```

#### Q3.4 时间范围查询
```
自然语言：过去 30 天内发布的文章有哪些？
期望 SQL：
SELECT title, published_at, view_count
FROM posts
WHERE status = 'published'
  AND published_at >= CURRENT_DATE - INTERVAL '30 days'
ORDER BY published_at DESC;
```

#### Q3.5 嵌套评论查询
```
自然语言：显示所有评论及其回复
期望 SQL：
SELECT
    c1.content as comment,
    u1.username as commenter,
    c2.content as reply,
    u2.username as replier
FROM comments c1
JOIN users u1 ON c1.user_id = u1.id
LEFT JOIN comments c2 ON c1.id = c2.parent_id
LEFT JOIN users u2 ON c2.user_id = u2.id
WHERE c1.parent_id IS NULL;
```

### Level 4: 高级查询 (视图 + 复杂逻辑)

#### Q4.1 使用视图
```
自然语言：显示所有已发布文章的统计信息
期望 SQL：
SELECT * FROM published_posts
ORDER BY published_at DESC;
```

#### Q4.2 用户活跃度分析
```
自然语言：哪些用户既发表了文章又发表了评论？
期望 SQL：
SELECT u.username, u.full_name
FROM users u
WHERE EXISTS (SELECT 1 FROM posts p WHERE p.author_id = u.id)
  AND EXISTS (SELECT 1 FROM comments c WHERE c.user_id = u.id);
```

#### Q4.3 标签流行度分析
```
自然语言：最常用的 5 个标签是什么？
期望 SQL：
SELECT * FROM popular_tags LIMIT 5;
或：
SELECT t.name, COUNT(pt.post_id) as usage_count
FROM tags t
JOIN post_tags pt ON t.id = pt.tag_id
JOIN posts p ON pt.post_id = p.id
WHERE p.status = 'published'
GROUP BY t.id, t.name
ORDER BY usage_count DESC
LIMIT 5;
```

#### Q4.4 用户参与度排名
```
自然语言：按总活跃度（文章数+评论数）排序用户
期望 SQL：
SELECT * FROM user_stats
ORDER BY (post_count + comment_count) DESC;
```

#### Q4.5 时间段活跃分析
```
自然语言：每个月发布了多少篇文章？
期望 SQL：
SELECT
    DATE_TRUNC('month', published_at) as month,
    COUNT(*) as post_count
FROM posts
WHERE status = 'published'
GROUP BY DATE_TRUNC('month', published_at)
ORDER BY month DESC;
```

---

## Medium Database (ecommerce_medium)

### 📊 数据库结构
- 24 张表：用户、商品、订单、支付、库存、评价、购物车、优惠券等
- 6 个视图：库存视图、客户统计、每日销售等
- 测试数据：10 用户, 15 商品, 7 订单

### Level 1: 简单查询

#### Q1.1 基础数据查询
```
自然语言：有多少个商品？
期望 SQL：SELECT COUNT(*) FROM products WHERE is_active = TRUE;
```

#### Q1.2 价格查询
```
自然语言：显示所有价格低于 50 美元的商品
期望 SQL：
SELECT name, price FROM products
WHERE price < 50 AND is_active = TRUE;
```

#### Q1.3 库存查询
```
自然语言：哪些商品有库存？
期望 SQL：
SELECT p.name, SUM(i.quantity) as total_stock
FROM products p
JOIN inventory i ON p.id = i.product_id
GROUP BY p.id, p.name
HAVING SUM(i.quantity) > 0;
```

#### Q1.4 订单状态
```
自然语言：有多少订单已发货？
期望 SQL：
SELECT COUNT(*) FROM orders WHERE status = 'shipped';
```

#### Q1.5 用户数据
```
自然语言：列出所有已验证邮箱的用户
期望 SQL：
SELECT email, first_name, last_name
FROM users
WHERE email_verified = TRUE;
```

### Level 2: 中等查询

#### Q2.1 商品分类统计
```
自然语言：每个分类下有多少商品？
期望 SQL：
SELECT c.name, COUNT(p.id) as product_count
FROM categories c
LEFT JOIN products p ON c.id = p.category_id AND p.is_active = TRUE
GROUP BY c.id, c.name
ORDER BY product_count DESC;
```

#### Q2.2 销售统计
```
自然语言：总销售额是多少？
期望 SQL：
SELECT SUM(total_amount) as total_revenue
FROM orders
WHERE status NOT IN ('cancelled', 'refunded');
```

#### Q2.3 热门商品
```
自然语言：哪些商品被购买次数最多？
期望 SQL：
SELECT
    p.name,
    COUNT(oi.id) as times_purchased,
    SUM(oi.quantity) as total_quantity
FROM products p
JOIN order_items oi ON p.id = oi.product_id
GROUP BY p.id, p.name
ORDER BY times_purchased DESC
LIMIT 10;
```

#### Q2.4 低库存预警
```
自然语言：哪些商品库存低于补货点？
期望 SQL：
SELECT * FROM low_stock_products;
```

#### Q2.5 订单金额分析
```
自然语言：平均订单金额是多少？
期望 SQL：
SELECT AVG(total_amount) as avg_order_value
FROM orders
WHERE status NOT IN ('cancelled', 'refunded');
```

### Level 3: 复杂查询

#### Q3.1 客户购买行为分析
```
自然语言：显示每个客户的订单数量和总消费
期望 SQL：
SELECT * FROM customer_order_summary
ORDER BY total_spent DESC;
```

#### Q3.2 商品评价分析
```
自然语言：评分最高的 5 个商品是什么？
期望 SQL：
SELECT * FROM product_ratings
WHERE review_count > 0
ORDER BY average_rating DESC, review_count DESC
LIMIT 5;
```

#### Q3.3 多仓库库存查询
```
自然语言：Laptop Pro 15 在各个仓库的库存情况
期望 SQL：
SELECT
    p.name,
    w.name as warehouse,
    i.quantity,
    i.reserved_quantity,
    (i.quantity - i.reserved_quantity) as available
FROM products p
JOIN inventory i ON p.id = i.product_id
JOIN warehouses w ON i.warehouse_id = w.id
WHERE p.name = 'Laptop Pro 15';
```

#### Q3.4 订单详情查询
```
自然语言：显示订单号 ORD-2024-0001 的完整信息
期望 SQL：
SELECT
    o.order_number,
    o.status,
    o.total_amount,
    u.email as customer_email,
    oi.product_name,
    oi.quantity,
    oi.unit_price
FROM orders o
JOIN users u ON o.user_id = u.id
JOIN order_items oi ON o.id = oi.order_id
WHERE o.order_number = 'ORD-2024-0001';
```

#### Q3.5 优惠券使用情况
```
自然语言：哪些优惠券被使用过，使用了多少次？
期望 SQL：
SELECT
    c.code,
    c.description,
    c.usage_count,
    c.usage_limit,
    SUM(cu.discount_amount) as total_discount_given
FROM coupons c
LEFT JOIN coupon_usage cu ON c.id = cu.coupon_id
GROUP BY c.id, c.code, c.description, c.usage_count, c.usage_limit
HAVING c.usage_count > 0
ORDER BY c.usage_count DESC;
```

### Level 4: 高级查询

#### Q4.1 每日销售趋势
```
自然语言：过去 7 天每天的销售情况
期望 SQL：
SELECT * FROM daily_sales
WHERE sale_date >= CURRENT_DATE - INTERVAL '7 days'
ORDER BY sale_date DESC;
```

#### Q4.2 复购率分析
```
自然语言：有多少客户下了多次订单？
期望 SQL：
SELECT
    COUNT(*) as repeat_customers,
    AVG(order_count) as avg_orders_per_customer
FROM (
    SELECT user_id, COUNT(*) as order_count
    FROM orders
    WHERE status NOT IN ('cancelled', 'refunded')
    GROUP BY user_id
    HAVING COUNT(*) > 1
) repeat_customer_stats;
```

#### Q4.3 商品组合分析
```
自然语言：哪些商品经常一起购买？
期望 SQL：
SELECT
    oi1.product_name as product1,
    oi2.product_name as product2,
    COUNT(*) as times_together
FROM order_items oi1
JOIN order_items oi2 ON oi1.order_id = oi2.order_id
    AND oi1.product_id < oi2.product_id
GROUP BY oi1.product_name, oi2.product_name
ORDER BY times_together DESC
LIMIT 10;
```

#### Q4.4 收入贡献分析
```
自然语言：哪些商品贡献了最多收入？
期望 SQL：
SELECT
    p.name,
    p.category_id,
    SUM(oi.total_price) as total_revenue,
    COUNT(DISTINCT oi.order_id) as order_count,
    SUM(oi.quantity) as units_sold
FROM products p
JOIN order_items oi ON p.id = oi.product_id
JOIN orders o ON oi.order_id = o.id
WHERE o.status NOT IN ('cancelled', 'refunded')
GROUP BY p.id, p.name, p.category_id
ORDER BY total_revenue DESC
LIMIT 10;
```

#### Q4.5 物流状态追踪
```
自然语言：所有在途订单的物流信息
期望 SQL：
SELECT
    o.order_number,
    s.tracking_number,
    sc.name as carrier,
    s.status as shipment_status,
    s.shipped_at,
    s.estimated_delivery,
    ua.city || ', ' || ua.state as destination
FROM orders o
JOIN shipments s ON o.id = s.order_id
JOIN shipping_carriers sc ON s.carrier_id = sc.id
JOIN user_addresses ua ON o.shipping_address_id = ua.id
WHERE s.status = 'in_transit';
```

---

## Large Database (saas_crm_large)

### 📊 数据库结构
- 45 张表：多租户架构，包含 CRM、销售、支持、计费等完整功能
- 10 个视图：销售管道、收入统计、工单指标等
- 测试数据：4 个组织, 9 用户, 6 客户账户

### Level 1: 简单查询

#### Q1.1 组织查询
```
自然语言：有多少个组织？
期望 SQL：SELECT COUNT(*) FROM organizations;
```

#### Q1.2 用户查询
```
自然语言：Acme Corporation 有多少个用户？
期望 SQL：
SELECT COUNT(*)
FROM users u
JOIN organizations o ON u.organization_id = o.id
WHERE o.name = 'Acme Corporation';
```

#### Q1.3 客户账户
```
自然语言：列出所有客户公司
期望 SQL：
SELECT name, industry, website
FROM accounts
ORDER BY name;
```

#### Q1.4 销售线索
```
自然语言：有多少个新线索？
期望 SQL：
SELECT COUNT(*) FROM leads WHERE status = 'new';
```

#### Q1.5 待办任务
```
自然语言：有多少个未完成的任务？
期望 SQL：
SELECT COUNT(*) FROM tasks
WHERE completed_at IS NULL;
```

### Level 2: 中等查询

#### Q2.1 销售机会统计
```
自然语言：每个销售阶段有多少个商机？
期望 SQL：
SELECT
    ps.name as stage,
    COUNT(d.id) as deal_count,
    SUM(d.amount) as total_value
FROM pipeline_stages ps
LEFT JOIN deals d ON ps.id = d.stage_id
GROUP BY ps.id, ps.name, ps.display_order
ORDER BY ps.display_order;
```

#### Q2.2 工单统计
```
自然语言：每个优先级有多少个开放工单？
期望 SQL：
SELECT
    priority,
    COUNT(*) as ticket_count
FROM tickets
WHERE status IN ('open', 'in_progress')
GROUP BY priority
ORDER BY
    CASE priority
        WHEN 'urgent' THEN 1
        WHEN 'high' THEN 2
        WHEN 'medium' THEN 3
        WHEN 'low' THEN 4
    END;
```

#### Q2.3 收入统计
```
自然语言：本月收入是多少？
期望 SQL：
SELECT SUM(total_amount) as monthly_revenue
FROM invoices
WHERE status = 'paid'
  AND DATE_TRUNC('month', paid_date) = DATE_TRUNC('month', CURRENT_DATE);
```

#### Q2.4 用户活跃度
```
自然语言：每个用户拥有多少个商机？
期望 SQL：
SELECT * FROM user_activity_summary
WHERE organization_id = 1
ORDER BY deals_owned DESC;
```

#### Q2.5 订阅统计
```
自然语言：有多少个活跃订阅？
期望 SQL：
SELECT COUNT(*) FROM subscriptions
WHERE status = 'active';
```

### Level 3: 复杂查询

#### Q3.1 销售管道分析
```
自然语言：显示 Acme Corporation 的销售管道概况
期望 SQL：
SELECT * FROM sales_pipeline_summary
WHERE organization_id = (
    SELECT id FROM organizations WHERE name = 'Acme Corporation'
)
ORDER BY pipeline_id, stage_type;
```

#### Q3.2 客户价值分析
```
自然语言：哪些客户带来的收入最多？
期望 SQL：
SELECT * FROM account_revenue
WHERE organization_id = 1
ORDER BY total_paid DESC
LIMIT 10;
```

#### Q3.3 商机转化分析
```
自然语言：过去 30 天内赢得了哪些商机？
期望 SQL：
SELECT
    d.name as deal_name,
    a.name as account_name,
    d.amount,
    d.actual_close_date,
    u.full_name as owner
FROM deals d
JOIN accounts a ON d.account_id = a.id
JOIN users u ON d.owner_id = u.id
JOIN pipeline_stages ps ON d.stage_id = ps.id
WHERE ps.stage_type = 'closed_won'
  AND d.actual_close_date >= CURRENT_DATE - INTERVAL '30 days'
ORDER BY d.actual_close_date DESC;
```

#### Q3.4 工单解决时效
```
自然语言：平均工单解决时间是多少？
期望 SQL：
SELECT
    priority,
    COUNT(*) as resolved_count,
    AVG(EXTRACT(EPOCH FROM (resolved_at - created_at)) / 3600)::NUMERIC(10,2) as avg_resolution_hours
FROM tickets
WHERE resolved_at IS NOT NULL
GROUP BY priority;
```

#### Q3.5 活动跟踪
```
自然语言：本周计划了哪些销售活动？
期望 SQL：
SELECT
    a.activity_type,
    a.subject,
    a.scheduled_at,
    u.full_name as owner,
    acc.name as account_name
FROM activities a
JOIN users u ON a.owner_id = u.id
LEFT JOIN accounts acc ON a.account_id = acc.id
WHERE a.scheduled_at BETWEEN DATE_TRUNC('week', CURRENT_DATE)
    AND DATE_TRUNC('week', CURRENT_DATE) + INTERVAL '7 days'
ORDER BY a.scheduled_at;
```

### Level 4: 高级查询

#### Q4.1 月度经常性收入 (MRR)
```
自然语言：计算每个组织的月度经常性收入
期望 SQL：
SELECT
    o.name as organization,
    COUNT(s.id) as active_subscriptions,
    SUM(CASE
        WHEN sp.billing_interval = 'month' THEN sp.price
        WHEN sp.billing_interval = 'year' THEN sp.price / 12
        ELSE 0
    END) as mrr
FROM organizations o
LEFT JOIN subscriptions s ON o.id = s.organization_id
    AND s.status = 'active'
LEFT JOIN subscription_plans sp ON s.plan_id = sp.id
GROUP BY o.id, o.name
ORDER BY mrr DESC;
```

#### Q4.2 销售漏斗转化率
```
自然语言：计算从线索到成交的转化率
期望 SQL：
SELECT
    l.source,
    COUNT(*) as total_leads,
    COUNT(CASE WHEN l.status = 'qualified' THEN 1 END) as qualified_leads,
    COUNT(l.converted_at) as converted_leads,
    ROUND(COUNT(l.converted_at)::NUMERIC / COUNT(*) * 100, 2) as conversion_rate
FROM leads l
WHERE l.organization_id = 1
GROUP BY l.source
ORDER BY conversion_rate DESC;
```

#### Q4.3 客户生命周期价值
```
自然语言：计算每个客户的总价值（订阅+发票）
期望 SQL：
SELECT
    a.name as account_name,
    COUNT(DISTINCT s.id) as subscription_count,
    COUNT(DISTINCT i.id) as invoice_count,
    COALESCE(SUM(i.total_amount), 0) as total_invoiced,
    COALESCE(SUM(CASE WHEN i.status = 'paid' THEN i.total_amount ELSE 0 END), 0) as total_paid
FROM accounts a
LEFT JOIN subscriptions s ON a.id = s.account_id
LEFT JOIN invoices i ON a.id = i.account_id
WHERE a.organization_id = 1
GROUP BY a.id, a.name
ORDER BY total_paid DESC;
```

#### Q4.4 团队绩效分析
```
自然语言：比较各个销售代表的业绩
期望 SQL：
SELECT
    u.full_name as sales_rep,
    COUNT(DISTINCT d.id) as total_deals,
    COUNT(DISTINCT CASE WHEN ps.stage_type = 'closed_won' THEN d.id END) as won_deals,
    SUM(CASE WHEN ps.stage_type = 'closed_won' THEN d.amount ELSE 0 END) as won_amount,
    ROUND(
        COUNT(DISTINCT CASE WHEN ps.stage_type = 'closed_won' THEN d.id END)::NUMERIC /
        NULLIF(COUNT(DISTINCT d.id), 0) * 100,
        2
    ) as win_rate
FROM users u
LEFT JOIN deals d ON u.id = d.owner_id
LEFT JOIN pipeline_stages ps ON d.stage_id = ps.id
WHERE u.role = 'sales_rep' AND u.organization_id = 1
GROUP BY u.id, u.full_name
ORDER BY won_amount DESC;
```

#### Q4.5 营销活动 ROI
```
自然语言：哪些营销活动的响应率最高？
期望 SQL：
SELECT * FROM campaign_performance
WHERE organization_id = 1 AND total_recipients > 0
ORDER BY response_rate DESC;
```

#### Q4.6 逾期发票追踪
```
自然语言：哪些客户有逾期未付的发票？
期望 SQL：
SELECT
    a.name as account_name,
    i.invoice_number,
    i.total_amount,
    i.due_date,
    (CURRENT_DATE - i.due_date) as days_overdue,
    c.first_name || ' ' || c.last_name as primary_contact
FROM invoices i
JOIN accounts a ON i.account_id = a.id
LEFT JOIN contacts c ON a.id = c.account_id AND c.is_primary = TRUE
WHERE i.status = 'overdue'
  AND i.organization_id = 1
ORDER BY days_overdue DESC;
```

#### Q4.7 产品销售分析
```
自然语言：哪些产品最常出现在成交的商机中？
期望 SQL：
SELECT
    p.name as product_name,
    COUNT(DISTINCT dp.deal_id) as deal_count,
    SUM(dp.quantity) as total_quantity,
    SUM(dp.total_price) as total_revenue
FROM products p
JOIN deal_products dp ON p.id = dp.product_id
JOIN deals d ON dp.deal_id = d.id
JOIN pipeline_stages ps ON d.stage_id = ps.id
WHERE ps.stage_type = 'closed_won'
  AND p.organization_id = 1
GROUP BY p.id, p.name
ORDER BY total_revenue DESC;
```

#### Q4.8 支持工单趋势分析
```
自然语言：过去 3 个月的工单趋势如何？
期望 SQL：
SELECT
    DATE_TRUNC('week', created_at) as week,
    COUNT(*) as tickets_created,
    COUNT(CASE WHEN resolved_at IS NOT NULL THEN 1 END) as tickets_resolved,
    AVG(
        CASE WHEN resolved_at IS NOT NULL
        THEN EXTRACT(EPOCH FROM (resolved_at - created_at)) / 3600
        END
    )::NUMERIC(10,2) as avg_resolution_hours
FROM tickets
WHERE created_at >= CURRENT_DATE - INTERVAL '3 months'
  AND organization_id = 1
GROUP BY DATE_TRUNC('week', created_at)
ORDER BY week DESC;
```

---

## 跨难度综合测试

### 边界情况测试

#### E1 空结果处理
```
自然语言：有没有价格超过 10000 美元的商品？
期望行为：即使没有结果也应正确返回空集
```

#### E2 NULL 值处理
```
自然语言：哪些商机还没有预计成交日期？
期望 SQL：SELECT * FROM deals WHERE expected_close_date IS NULL;
```

#### E3 模糊搜索
```
自然语言：找出名字包含"John"的所有用户
期望 SQL：
SELECT * FROM users
WHERE first_name ILIKE '%John%' OR last_name ILIKE '%John%';
```

#### E4 日期范围
```
自然语言：上周创建的订单
期望 SQL：
SELECT * FROM orders
WHERE created_at >= DATE_TRUNC('week', CURRENT_DATE - INTERVAL '1 week')
  AND created_at < DATE_TRUNC('week', CURRENT_DATE);
```

#### E5 百分比计算
```
自然语言：已发货订单占总订单的百分比是多少？
期望 SQL：
SELECT
    ROUND(
        COUNT(CASE WHEN status = 'shipped' THEN 1 END)::NUMERIC /
        COUNT(*) * 100,
        2
    ) as shipped_percentage
FROM orders;
```

### 性能测试查询

#### P1 大结果集
```
自然语言：列出所有数据（应该触发行数限制）
期望行为：应用 LIMIT 限制，避免返回过多数据
```

#### P2 复杂计算
```
自然语言：计算每个客户的终身价值和购买频率
期望行为：测试多表 JOIN 和复杂聚合的性能
```

#### P3 深度嵌套
```
自然语言：找出有评论回复的评论的回复（三层嵌套）
期望行为：测试递归或自连接查询
```

### 安全测试

#### S1 SQL 注入尝试（应被拦截）
```
自然语言：显示所有用户'; DROP TABLE users; --
期望行为：安全验证应拒绝执行
```

#### S2 敏感表访问（如果配置了黑名单）
```
自然语言：显示用户密码
期望行为：应拒绝访问敏感列
```

#### S3 修改操作（应被拦截）
```
自然语言：删除所有草稿文章
期望行为：SQL 验证器应拒绝 DELETE 语句
```

### 歧义消解测试

#### A1 时间歧义
```
自然语言：本月的订单
问题：是当前月份还是过去 30 天？
期望：系统应选择合理的解释或询问用户
```

#### A2 实体歧义
```
自然语言：显示 admin 的信息
问题：是用户名为 "admin" 还是角色为 admin 的用户？
期望：基于上下文做出合理推断
```

#### A3 度量歧义
```
自然语言：最贵的商品
问题：是按原价还是按折扣价？
期望：选择最常用的解释（通常是当前售价）
```

---

## 测试建议

### 测试流程
1. **从简单到复杂**：按 Level 1 → 2 → 3 → 4 顺序测试
2. **按数据库分组**：先完成一个数据库的所有级别，再测试下一个
3. **验证结果**：
   - SQL 语法正确性
   - 查询结果准确性
   - 执行性能（响应时间）
   - 置信度评分

### 成功标准
- **Level 1**：95%+ 准确率（基础查询应几乎全对）
- **Level 2**：85%+ 准确率（中等查询允许小错误）
- **Level 3**：75%+ 准确率（复杂查询可能需要多次尝试）
- **Level 4**：60%+ 准确率（高级查询可能需要人工优化）

### 评估维度
1. **SQL 正确性**：生成的 SQL 是否语法正确
2. **语义准确性**：SQL 是否正确理解了用户意图
3. **查询效率**：是否使用了合适的索引和 JOIN 策略
4. **安全性**：是否正确拦截了危险操作
5. **可读性**：生成的 SQL 是否清晰易懂

---

## 附录：常见查询模式

### 时间相关
- "今天" → `CURRENT_DATE`
- "本周" → `DATE_TRUNC('week', CURRENT_DATE)`
- "本月" → `DATE_TRUNC('month', CURRENT_DATE)`
- "过去 X 天" → `>= CURRENT_DATE - INTERVAL 'X days'`

### 排序相关
- "最多" → `ORDER BY ... DESC LIMIT`
- "最少" → `ORDER BY ... ASC LIMIT`
- "前 N 个" → `LIMIT N`

### 聚合相关
- "总共" → `SUM()`
- "平均" → `AVG()`
- "最大/最小" → `MAX()` / `MIN()`
- "计数" → `COUNT()`

### 比较相关
- "超过" → `>`
- "低于" → `<`
- "至少" → `>=`
- "最多" → `<=`
