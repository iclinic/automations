<?php

/**
 * Por que esta fixture existe
 * ---------------------------
 *
 * Arquivo truncado: o corpo de `up()` não fecha. O `DROP COLUMN` está escrito,
 * mas o parser não delimitou o corpo e não pode afirmar quais statements estão
 * dentro dele. `unknown`, e não a leitura do que deu para ler antes do corte.
 */

declare(strict_types=1);

namespace Migrations;

use Doctrine\DBAL\Schema\Schema;
use Doctrine\Migrations\AbstractMigration;

final class Version20250201120700 extends AbstractMigration
{
    public function up(Schema $schema): void
    {
        $this->addSql('ALTER TABLE stock."warehouse_bin" DROP COLUMN "capacity"');
