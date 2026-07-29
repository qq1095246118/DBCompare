BEGIN;

SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '60s';

WITH desired (
    token_symbol,
    chain,
    chain_name,
    token_address,
    coingecko_coin_id,
    platform_id,
    reference
) AS (
    VALUES
        ('M', 'bsc', 'BNB Smart Chain (BEP20)', '0x22b1458e780f8fa71e2f84502cee8b5a3cc731fa', 'memecore', 'binance-smart-chain', 'coingecko_platforms'),
        ('BEAT', 'bsc', 'BNB Smart Chain (BEP20)', '0xcf3232b85b43bca90e51d38cc06cc8bb8c8a3e36', 'audiera', 'binance-smart-chain', 'coingecko_platforms'),
        ('B', 'bsc', 'BNB Smart Chain (BEP20)', '0x6bdcce4a559076e37755a78ce0c06214e59e4444', 'build-on', 'binance-smart-chain', 'coingecko_platforms'),
        ('DEXE', 'eth', 'Ethereum (ERC20)', '0xde4ee8057785a7e8e800db58f9784845a5c2cbd6', 'dexe', 'ethereum', 'coingecko_platforms'),
        ('VELVET', 'base', 'Base', '0xbf927b841994731c573bdf09ceb0c6b0aa887cdd', 'velvet', 'base', 'coingecko_platforms'),
        ('VELVET', 'bsc', 'BNB Smart Chain (BEP20)', '0x8b194370825e37b33373e74a41009161808c1488', 'velvet', 'binance-smart-chain', 'coingecko_platforms'),
        ('H', 'eth', 'Ethereum (ERC20)', '0xe76c5b78f93909d34404e9eb4c1f19e7582a5de1', 'humanity', 'ethereum', 'coingecko_platforms'),
        ('RIF', 'arbitrum', 'Arbitrum One', '0xe5e851b01dd3eda24fde709a407db44555b6d1e0', 'rif-token', 'arbitrum-one', 'coingecko_platforms'),
        ('RIF', 'base', 'Base', '0xe5e851b01dd3eda24fde709a407db44555b6d1e0', 'rif-token', 'base', 'coingecko_platforms'),
        ('RIF', 'eth', 'Ethereum (ERC20)', '0x01b603be3d545f096015741e6503440282bf45fb', 'rif-token', 'ethereum', 'coingecko_platforms'),
        ('RIF', 'solana', 'Solana', 'AAeENcfHbTExuTvs4q7r9Bjax98Dg6BGX3aMph4bTLdK', 'rif-token', 'solana', 'coingecko_platforms'),
        ('ALLO', 'eth', 'Ethereum (ERC20)', '0x8408d45b61f5823298f19a09b53b7339c0280489', 'allora', 'ethereum', 'coingecko_platforms'),
        ('TAIKO', 'bsc', 'BNB Smart Chain (BEP20)', '0x30c60b20c25b2810ca524810467a0c342294fc61', 'taiko', 'binance-smart-chain', 'coingecko_platforms'),
        ('TAIKO', 'eth', 'Ethereum (ERC20)', '0x10dea67478c5f8c5e2d90e5e9b26dbe60c54d800', 'taiko', 'ethereum', 'coingecko_platforms'),
        ('AKE', 'bsc', 'BNB Smart Chain (BEP20)', '0x2c3a8ee94ddd97244a93bc48298f97d2c412f7db', 'akedo', 'binance-smart-chain', 'coingecko_platforms'),
        ('TAC', 'bsc', 'BNB Smart Chain (BEP20)', '0x1219c409fabe2c27bd0d1a565daeed9bd9f271de', 'tac', 'binance-smart-chain', 'coingecko_platforms'),
        ('TAC', 'ton', 'TON', 'EQBE_gBrU3mPI9hHjlJoR_kYyrhQgyCFD6EUWfa42W8T7EBP', 'tac', 'the-open-network', 'coingecko_platforms'),
        ('ESPORTS', 'bsc', 'BNB Smart Chain (BEP20)', '0xf39e4b21c84e737df08e2c3b32541d856f508e48', 'yooldo-games', 'binance-smart-chain', 'coingecko_platforms'),
        ('EVAA', 'bsc', 'BNB Smart Chain (BEP20)', '0xaa036928c9c0df07d525b55ea8ee690bb5a628c1', 'evaa-protocol', 'binance-smart-chain', 'coingecko_platforms'),
        ('EVAA', 'ton', 'TON', 'EQBKMfjX_a_dsOLm-juxyVZytFP7_KKnzGv6J01kGc72gVBp', 'evaa-protocol', 'the-open-network', 'coingecko_platforms'),
        ('LAB', 'bsc', 'BNB Smart Chain (BEP20)', '0x7ec43cf65f1663f820427c62a5780b8f2e25593a', 'lab', 'binance-smart-chain', 'coingecko_platforms'),
        ('PORTAL', 'base', 'Base', '0x0ffebc403f2d3dd9ea5501ca03916e98967acb2d', 'portal-2', 'base', 'coingecko_platforms'),
        ('PORTAL', 'eth', 'Ethereum (ERC20)', '0x1bbe973bef3a977fc51cbed703e8ffdefe001fed', 'portal-2', 'ethereum', 'coingecko_platforms'),
        ('PORTAL', 'solana', 'Solana', 'FMQjDvT1GztVxdvYgMBEde4L54fftFGx9m5GmbqeJGM5', 'portal-2', 'solana', 'coingecko_platforms'),
        ('GUA', 'bsc', 'BNB Smart Chain (BEP20)', '0xa5c8e1513b6a08334b479fe4d71f1253259469be', 'superfortune', 'binance-smart-chain', 'coingecko_platforms'),
        ('AGT', 'bsc', 'BNB Smart Chain (BEP20)', '0x5dbde81fce337ff4bcaaee4ca3466c00aecae274', 'alaya-ai', 'binance-smart-chain', 'coingecko_platforms'),
        ('BANK', 'bsc', 'BNB Smart Chain (BEP20)', '0x3aee7602b612de36088f3ffed8c8f10e86ebf2bf', 'lorenzo-protocol', 'binance-smart-chain', 'coingecko_platforms'),
        ('BTW', 'bsc', 'BNB Smart Chain (BEP20)', '0x444045b0ee1ee319a660a5e3d604ca0ffa35acaa', 'bitway', 'binance-smart-chain', 'coingecko_platforms'),
        ('BTW', 'eth', 'Ethereum (ERC20)', '0x3a63de3572c69a1307ff08394f3ee7702c16d25d', 'bitway', 'ethereum', 'coingecko_platforms'),
        ('OPN', 'bsc', 'BNB Smart Chain (BEP20)', '0x7977bf3e7e0c954d12cdca3e013adaf57e0b06e0', 'opinion', 'binance-smart-chain', 'coingecko_platforms'),
        ('OPN', 'eth', 'Ethereum (ERC20)', '0x7977bf3e7e0c954d12cdca3e013adaf57e0b06e0', 'opinion', 'ethereum', 'coingecko_platforms'),
        ('SKYAI', 'bsc', 'BNB Smart Chain (BEP20)', '0x92aa03137385f18539301349dcfc9ebc923ffb10', 'skyai', 'binance-smart-chain', 'coingecko_platforms'),
        ('HEI', 'bsc', 'BNB Smart Chain (BEP20)', '0xf8f173e20e15f3b6cb686fb64724d370689de083', 'heima', 'binance-smart-chain', 'coingecko_platforms'),
        ('HEI', 'eth', 'Ethereum (ERC20)', '0xf8f173e20e15f3b6cb686fb64724d370689de083', 'heima', 'ethereum', 'coingecko_platforms'),
        ('BICO', 'arbitrum', 'Arbitrum One', '0xa68ec98d7ca870cf1dd0b00ebbb7c4bf60a8e74d', 'biconomy', 'arbitrum-one', 'coingecko_platforms'),
        ('BICO', 'eth', 'Ethereum (ERC20)', '0xf17e65822b568b3903685a7c9f496cf7656cc6c2', 'biconomy', 'ethereum', 'coingecko_platforms'),
        ('AGLD', 'eth', 'Ethereum (ERC20)', '0x32353a6c91143bfd6c7d363b546e62a9a2489a20', 'adventure-gold', 'ethereum', 'coingecko_platforms'),
        ('OPG', 'base', 'Base', '0xfbc2051ae2265686a469421b2c5a2d5462fbf5eb', 'opengradient', 'base', 'coingecko_platforms'),
        ('OPG', 'bsc', 'BNB Smart Chain (BEP20)', '0x5feccd17c393caf1001d18164236a37e731fcb9d', 'opengradient', 'binance-smart-chain', 'coingecko_platforms'),
        ('BR', 'base', 'Base', '0xd6122ddada244913521f3d62006eaf756c157660', 'bedrock-token', 'base', 'coingecko_platforms'),
        ('BR', 'bsc', 'BNB Smart Chain (BEP20)', '0xff7d6a96ae471bbcd7713af9cb1feeb16cf56b41', 'bedrock-token', 'binance-smart-chain', 'coingecko_platforms'),
        ('BR', 'eth', 'Ethereum (ERC20)', '0x9b61879e91a0b1322f3d61c23aaf936231882096', 'bedrock-token', 'ethereum', 'coingecko_platforms'),
        ('BIRB', 'solana', 'Solana', 'G7vQWurMkMMm2dU3iZpXYFTHT9Biio4F4gZCrwFpKNwG', 'moonbirds', 'solana', 'coingecko_platforms'),
        ('PLAY', 'base', 'Base', '0x853a7c99227499dba9db8c3a02aa691afdebf841', 'playsout', 'base', 'coingecko_platforms'),
        ('PLAY', 'bsc', 'BNB Smart Chain (BEP20)', '0xf86089b30f30285d492b0527c37b9c2225bfcf8c', 'playsout', 'binance-smart-chain', 'coingecko_platforms'),
        ('SYN', 'arbitrum', 'Arbitrum One', '0x080f6aed32fc474dd5717105dba5ea57268f46eb', 'synapse-2', 'arbitrum-one', 'coingecko_platforms'),
        ('SYN', 'avalanche', 'Avalanche C-Chain', '0x1f1e7c893855525b303f99bdf5c3c05be09ca251', 'synapse-2', 'avalanche', 'coingecko_platforms'),
        ('SYN', 'base', 'Base', '0x432036208d2717394d2614d6697c46df3ed69540', 'synapse-2', 'base', 'coingecko_platforms'),
        ('SYN', 'bsc', 'BNB Smart Chain (BEP20)', '0xa4080f1778e69467e905b8d6f72f6e441f9e9484', 'synapse-2', 'binance-smart-chain', 'coingecko_platforms'),
        ('SYN', 'polygon', 'Polygon POS', '0xf8f9efc0db77d8881500bb06ff5d6abc3070e695', 'synapse-2', 'polygon-pos', 'coingecko_platforms'),
        ('STG', 'arbitrum', 'Arbitrum One', '0x6694340fc020c5e6b96567843da2df01b2ce1eb6', 'stargate-finance', 'arbitrum-one', 'coingecko_platforms'),
        ('STG', 'avalanche', 'Avalanche C-Chain', '0x2f6f07cdcf3588944bf4c42ac74ff24bf56e7590', 'stargate-finance', 'avalanche', 'coingecko_platforms'),
        ('STG', 'base', 'Base', '0xe3b53af74a4bf62ae5511055290838050bf764df', 'stargate-finance', 'base', 'coingecko_platforms'),
        ('STG', 'bsc', 'BNB Smart Chain (BEP20)', '0xb0d502e938ed5f4df2e681fe6e419ff29631d62b', 'stargate-finance', 'binance-smart-chain', 'coingecko_platforms'),
        ('STG', 'eth', 'Ethereum (ERC20)', '0xaf5191b0de278c7286d6c7cc6ab6bb8a73ba2cd6', 'stargate-finance', 'ethereum', 'coingecko_platforms'),
        ('STG', 'polygon', 'Polygon POS', '0x2f6f07cdcf3588944bf4c42ac74ff24bf56e7590', 'stargate-finance', 'polygon-pos', 'coingecko_platforms'),
        ('JCT', 'bsc', 'BNB Smart Chain (BEP20)', '0xea37a8de1de2d9d10772eeb569e28bfa5cb17707', 'janction', 'binance-smart-chain', 'coingecko_platforms'),
        ('JCT', 'eth', 'Ethereum (ERC20)', '0xc477b6dfd26ec2460b3b92de18837fd476ea7549', 'janction', 'ethereum', 'coingecko_platforms'),
        ('BROCCOLIF3B', 'bsc', 'BNB Smart Chain (BEP20)', '0x12b4356c65340fb02cdff01293f95febb1512f3b', 'broccoli', 'binance-smart-chain', 'coingecko_platforms'),
        ('XNY', 'bsc', 'BNB Smart Chain (BEP20)', '0xe3225e11cab122f1a126a28997788e5230838ab9', 'codatta', 'binance-smart-chain', 'coingecko_platforms'),
        ('UB', 'bsc', 'BNB Smart Chain (BEP20)', '0x40b8129b786d766267a7a118cf8c07e31cdb6fde', 'unibase', 'binance-smart-chain', 'coingecko_platforms'),
        ('UB', 'eth', 'Ethereum (ERC20)', '0x6944e1df6bf5972305f9ab25df47ef10de01bcc8', 'unibase', 'ethereum', 'coingecko_platforms'),
        ('BLESS', 'bsc', 'BNB Smart Chain (BEP20)', '0x7c8217517ed4711fe2deccdfeffe8d906b9ae11f', 'bless-2', 'binance-smart-chain', 'coingecko_platforms'),
        ('BLESS', 'solana', 'Solana', 'A1t2UviBYwyfYZDJyKY2W6Td8ritgsCriUDuNaAQN49S', 'bless-2', 'solana', 'coingecko_platforms'),
        ('HMSTR', 'ton', 'TON', 'EQAJ8uWd7EBqsmpSWaRdf_I-8R8-XHwh3gsNKhy-UrdrPcUo', 'hamster-kombat', 'the-open-network', 'coingecko_platforms')
)
INSERT INTO public.binance_address_metadata AS target (
    token_symbol,
    chain,
    chain_name,
    token_address,
    source_name,
    coingecko_coin_id,
    platform_id,
    is_active,
    raw_payload
)
SELECT
    desired.token_symbol,
    desired.chain,
    desired.chain_name,
    desired.token_address,
    'manual_validation',
    desired.coingecko_coin_id,
    desired.platform_id,
    1,
    jsonb_build_object(
        'manual_audit',
        jsonb_build_object(
            'audit_date', '2026-07-27',
            'reference', desired.reference
        )
    )
FROM desired
ON CONFLICT (token_symbol, chain) DO UPDATE
SET
    chain_name = EXCLUDED.chain_name,
    token_address = EXCLUDED.token_address,
    coingecko_coin_id = EXCLUDED.coingecko_coin_id,
    platform_id = EXCLUDED.platform_id,
    is_active = 1,
    raw_payload = target.raw_payload || EXCLUDED.raw_payload,
    updated_at = CURRENT_TIMESTAMP
WHERE target.chain_name IS DISTINCT FROM EXCLUDED.chain_name
   OR target.token_address IS DISTINCT FROM EXCLUDED.token_address
   OR target.coingecko_coin_id IS DISTINCT FROM EXCLUDED.coingecko_coin_id
   OR target.platform_id IS DISTINCT FROM EXCLUDED.platform_id
   OR target.is_active IS DISTINCT FROM 1;

COMMIT;
